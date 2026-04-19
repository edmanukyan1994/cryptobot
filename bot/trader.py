import aiohttp
import asyncio
import logging
import json
from datetime import datetime, timezone

from config import TRADING_INTERVAL, DEMO_INITIAL_BALANCE, TELEGRAM_CHAT_ID, BYBIT_SYMBOL_MAP
import db
import telegram_bot as tg
from forecaster import get_latest_forecast
from sr_engine import analyze_sr, get_sr_entry_signal, update_features_sr
import scoring

logger = logging.getLogger("trader")

MAX_OPEN = 100
MAX_NEW_PER_CYCLE = 2
SCALP_MAX_NEW_PER_CYCLE = 5

SECTOR = {
    "BTC": "btc", "ETH": "eth", "BNB": "exchange",
    "SOL": "alt-l1", "ADA": "alt-l1", "AVAX": "alt-l1", "DOT": "alt-l1",
    "XRP": "payments", "TRX": "payments", "LTC": "payments", "XLM": "payments",
    "DOGE": "meme", "SHIB": "meme", "PEPE": "meme", "BONK": "meme", "WIF": "meme",
    "LINK": "oracle", "UNI": "defi", "AAVE": "defi", "ARB": "l2", "OP": "l2",
    "NEAR": "alt-l1", "APT": "alt-l1", "SUI": "alt-l1", "ATOM": "alt-l1",
}


def get_allowed_direction(fg: float) -> str:
    return "both"


def normalize_setup_type(setup_type: str) -> str:
    s = str(setup_type or "").strip().lower()
    mapping = {
        "impulse_short": "short_impulse",
        "impulse_long": "long_impulse",
        "short_impulse": "short_impulse",
        "long_impulse": "long_impulse",
        "long_reversal": "long_reversal",
        "long_trend": "long_trend",
        "long_support": "long_support",
        "short_trend": "short_trend",
        "normal": "normal",
        "": "normal",
    }
    return mapping.get(s, s or "normal")


def normalize_direction(value: str) -> str:
    v = str(value or "").lower().strip()
    if v in ("long", "up", "bull", "bullish", "buy"):
        return "long"
    if v in ("short", "down", "bear", "bearish", "sell"):
        return "short"
    return ""


def volume_to_bucket(volume: float) -> str:
    try:
        v = float(volume or 0)
    except Exception:
        v = 0.0

    if v >= 500_000_000:
        return "ultra"
    if v >= 100_000_000:
        return "high"
    if v >= 10_000_000:
        return "medium"
    if v >= 1_000_000:
        return "low"
    return "trash"


def liquidity_factor(features: dict) -> float:
    bucket = str(features.get("volume_bucket") or "")
    if not bucket:
        bucket = volume_to_bucket(features.get("volume_24h"))

    if bucket == "ultra":
        return 1.00
    if bucket == "high":
        return 0.85
    if bucket == "medium":
        return 0.65
    if bucket == "low":
        return 0.40
    if bucket == "trash":
        return 0.20
    return 0.50


def calc_position_size(
    balance: float,
    params: dict,
    features: dict,
    setup_type: str,
    forecast_probability: float = 0.0,
) -> float:
    base_pct = float(params.get("position_size_percent") or 5.0)
    max_single_pct = float(params.get("max_single_position_percent") or 7.0)

    size = balance * base_pct / 100.0

    # Ликвидность
    liq_factor = liquidity_factor(features)
    size *= liq_factor

    # Волатильность
    vol_bucket = str(features.get("volatility_bucket") or "unknown")
    if vol_bucket == "extreme":
        size *= 0.60
    elif vol_bucket == "high":
        size *= 0.80

    # Тип сетапа
    st = normalize_setup_type(setup_type)
    if st == "long_impulse":
        size *= 0.90
    elif st == "short_impulse":
        size *= 1.00
    elif st == "long_trend":
        size *= 0.90
    elif st == "short_trend":
        size *= 0.95
    elif st == "normal":
        size *= 0.80

    # Probability теперь влияет только на размер, а не на сам факт входа
    prob = float(forecast_probability or 0)

    if prob >= 80:
        size *= 1.15
    elif prob >= 70:
        size *= 1.00
    elif prob >= 60:
        size *= 0.85
    else:
        size *= 0.70

    size = min(size, balance * max_single_pct / 100.0)

    return round(size, 2)


def detect_setup_type(features: dict, forecast: dict) -> str:
    """
    Сначала берём setup_type из features_snapshot forecaster'а.
    Если его нет — fallback на старую эвристику.
    """
    snapshot = forecast.get("features_snapshot") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except Exception:
            snapshot = {}

    snap_setup = normalize_setup_type(snapshot.get("setup_type"))
    if snap_setup != "normal":
        return snap_setup

    direction = normalize_direction(forecast.get("direction"))
    r_1h = float(features.get("r_1h") or 0)
    rsi = float(features.get("rsi_14") or 50)
    sr_signal = str(features.get("sr_signal") or "")
    volume = float(features.get("volume_24h") or 0)
    relative_strength = float(features.get("relative_strength") or 0)
    impulse_score = int(features.get("impulse_score") or 0)
    reversal_score = int(features.get("reversal_score") or 0)
    market_mode = str(features.get("market_mode") or "sideways")
    dist_to_support = features.get("distance_to_support_pct")

    if direction == "short":
        if (
            impulse_score >= 3
            and r_1h <= -0.02
            and sr_signal in ("bounce_resistance", "breakout_down", "retest_broken_support_short", "neutral")
            and relative_strength <= 0.5
            and volume >= 1_000_000
        ):
            return "short_impulse"

        if market_mode == "bear":
            return "short_trend"

    if direction == "long":
        # LONG SUPPORT (новая логика)
        if dist_to_support is not None and dist_to_support <= 2.0:
            if rsi <= 55 and volume >= 700_000:
                if sr_signal in ("bounce_support", "neutral"):
                    return "long_support"

        if (
            reversal_score >= 2
            and (rsi <= 40 or sr_signal in ("bounce_support", "retest_broken_resistance_long"))
            and volume >= 1_000_000
        ):
            return "long_reversal"

        if (
            impulse_score >= 3
            and r_1h >= 0.02
            and relative_strength >= 1.0
            and volume >= 1_000_000
        ):
            return "long_impulse"

        if market_mode in ("bull", "bull_sideways"):
            return "long_trend"

    return "normal"


def detect_market_mode(features: dict, forecast: dict) -> str:
    feature_mode = str(features.get("market_mode") or "").strip()
    if feature_mode:
        return feature_mode

    forecast_snapshot = forecast.get("features_snapshot") or {}
    if isinstance(forecast_snapshot, str):
        try:
            forecast_snapshot = json.loads(forecast_snapshot)
        except Exception:
            forecast_snapshot = {}

    feature_snapshot_mode = str(forecast_snapshot.get("market_mode") or "").strip()
    if feature_snapshot_mode:
        return feature_snapshot_mode

    btc_ctx = forecast_snapshot.get("btc_context") or {}

    btc_regime = str(
        forecast_snapshot.get("btc_regime")
        or btc_ctx.get("global_regime")
        or ""
    )
    btc_structure = str(
        forecast_snapshot.get("btc_structure_4h")
        or btc_ctx.get("price_structure_4h")
        or ""
    )

    if btc_regime == "bear_market":
        if btc_structure == "downtrend":
            return "bear"
        return "bear_sideways"

    if btc_regime == "mild_bear":
        return "bear_sideways"

    if btc_regime == "crash":
        return "bear"

    if btc_regime == "bull_market":
        if btc_structure == "uptrend":
            return "bull"
        return "bull_sideways"

    if btc_regime == "mild_bull":
        return "bull_sideways"

    return "sideways"


def btc_move_allows_entry(
    setup_type: str,
    market_mode: str,
    btc_momentum: str,
    prob: float,
    relative_strength: float
) -> tuple[bool, str]:

    st = normalize_setup_type(setup_type)
    btc_mom = str(btc_momentum or "").lower()

    if st == "long_reversal":
        if btc_mom == "strong_down":
            return False, "btc_strong_down_block_reversal"

    if st == "long_impulse":
        if btc_mom == "strong_down":
            return False, "btc_strong_down_block"
        if btc_mom == "weak_down" and (prob < 72 or relative_strength < 1.0):
            return False, "btc_weak_down_filter"
        return True, "ok"

    if st in ("long_trend", "long_support"):
        if btc_mom == "strong_down":
            return False, "btc_down_block_trend"
        return True, "ok"

    if st == "short_trend":
        if btc_mom == "strong_down":
            return True, "btc_strong_down_boost"
        if btc_mom in ("strong_up", "weak_up"):
            return False, "btc_up_block_short_trend"
        return True, "ok"

    if st == "short_impulse":
        if btc_mom == "strong_down":
            return True, "btc_strong_down_boost"
        if btc_mom == "strong_up":
            return False, "btc_strong_up_block"
        if btc_mom == "weak_up" and (prob < 80 or relative_strength > 0):
            return False, "btc_weak_up_filter"
        return True, "ok"

    return True, "ok"



def detect_direction(features: dict, forecast: dict) -> tuple[str, str]:
    """
    Определяет направление входа на основе совокупности сигналов.
    Возвращает (direction, reason) или ("", reason) если направление не определено.

    Логика: считаем очки за каждый бычий/медвежий сигнал.
    Если перевес > MIN_EDGE очков — входим в этом направлении.

    Правило: SR говорит ГДЕ цена, остальные сигналы говорят КУДА пойдёт.
    """
    bull_score = 0
    bear_score = 0
    signals = []

    sr = str(features.get("sr_signal") or "neutral")
    candle = str(features.get("candlestick_pattern") or "none")
    rsi = float(features.get("rsi_14") or 50)
    r_1h = float(features.get("r_1h") or 0)
    ms = str(features.get("ms_structure") or "ranging")
    fib_zone = features.get("fib_zone")
    fib_dir = features.get("fib_direction")
    fib_sl = int(features.get("fib_score_long") or 0)
    fib_ss = int(features.get("fib_score_short") or 0)
    in_bull_fvg = bool(features.get("in_bullish_fvg"))
    in_bear_fvg = bool(features.get("in_bearish_fvg"))
    cs_long = int(features.get("candle_score_long") or 0)
    cs_short = int(features.get("candle_score_short") or 0)
    rs = float(features.get("relative_strength") or 0)

    # 1. SR СИГНАЛ — контекст (не направление!)
    # Пробои — сильный сигнал направления
    if sr == "breakout_up":
        bull_score += 30
        signals.append("breakout_up")
    elif sr == "breakout_down":
        bear_score += 30
        signals.append("breakout_down")
    # Отскоки — нейтральный контекст (только подтверждает свечу)
    # Не даём очков т.к. SR нестабилен между обновлениями
    elif sr == "bounce_support":
        signals.append("sr_support")
    elif sr == "bounce_resistance":
        signals.append("sr_resistance")

    # 2. СВЕЧНОЙ ПАТТЕРН — сильный сигнал подтверждения
    bullish_candles = ("rejection_low", "hammer", "inverted_hammer", "bullish_marubozu", "bullish_engulfing")
    bearish_candles = ("rejection_high", "shooting_star", "hanging_man", "bearish_marubozu", "bearish_engulfing")

    if candle in bullish_candles:
        # Вес зависит от SR контекста
        weight = 25 if sr == "bounce_support" else 15
        bull_score += weight
        signals.append(f"bullish_candle({candle})")
    elif candle in bearish_candles:
        weight = 25 if sr == "bounce_resistance" else 15
        bear_score += weight
        signals.append(f"bearish_candle({candle})")

    # 3. RSI — подтверждение перекупленности/перепроданности
    if rsi <= 30:
        bull_score += 20
        signals.append(f"oversold_rsi({rsi:.0f})")
    elif rsi <= 40:
        bull_score += 10
        signals.append(f"low_rsi({rsi:.0f})")
    elif rsi >= 70:
        bear_score += 20
        signals.append(f"overbought_rsi({rsi:.0f})")
    elif rsi >= 60:
        bear_score += 10
        signals.append(f"high_rsi({rsi:.0f})")

    # 4. ФИБОНАЧЧИ — определяет направление отката
    if fib_zone and fib_dir:
        if fib_dir == "bullish_retracement" and fib_sl > 0:
            # Откат в бычьем тренде — продолжение вверх
            weight = {"golden": 25, "half": 15, "shallow": 8, "deep": 10, "weak": 5}.get(fib_zone, 5)
            bull_score += weight
            signals.append(f"fib_{fib_zone}_bull")
        elif fib_dir == "bearish_retracement" and fib_ss > 0:
            # Откат в медвежьем тренде — продолжение вниз
            weight = {"golden": 25, "half": 15, "shallow": 8, "deep": 10, "weak": 5}.get(fib_zone, 5)
            bear_score += weight
            signals.append(f"fib_{fib_zone}_bear")

    # 5. FVG — цена в зоне дисбаланса
    if in_bull_fvg:
        bull_score += 20
        signals.append("in_bullish_fvg")
    if in_bear_fvg:
        bear_score += 20
        signals.append("in_bearish_fvg")

    # 6. MARKET STRUCTURE — общий тренд
    if ms == "uptrend":
        bull_score += 10
        signals.append("uptrend")
    elif ms == "downtrend":
        bear_score += 10
        signals.append("downtrend")

    # 7. ИМПУЛЬС 1H — краткосрочное давление
    if r_1h >= 0.2:
        bull_score += 10
        signals.append(f"bull_momentum({r_1h:.2f}%)")
    elif r_1h <= -0.2:
        bear_score += 10
        signals.append(f"bear_momentum({r_1h:.2f}%)")

    # 8. RELATIVE STRENGTH vs BTC
    if rs >= 0.5:
        bull_score += 8
        signals.append(f"strong_vs_btc({rs:.1f})")
    elif rs <= -0.5:
        bear_score += 8
        signals.append(f"weak_vs_btc({rs:.1f})")

    # Также учитываем forecaster как один из голосов
    fc_dir = normalize_direction(forecast.get("direction"))
    fc_prob = float(forecast.get("direction_probability") or 50)
    if fc_dir == "long" and fc_prob >= 60:
        bull_score += 8
        signals.append(f"fc_up({fc_prob:.0f}%)")
    elif fc_dir == "short" and fc_prob >= 60:
        bear_score += 8
        signals.append(f"fc_down({fc_prob:.0f}%)")

    # Определяем направление
    # Минимальный перевес для входа: 20 очков
    MIN_EDGE = 12
    edge = bull_score - bear_score

    if edge >= MIN_EDGE:
        # Блокируем если свечной скор сильно против лонга
        if cs_long < -15:
            return "", f"candle_blocks_long(cs={cs_long})"
        reason = f"bull({bull_score}) vs bear({bear_score}): {','.join(signals[:4])}"
        return "long", reason
    elif edge <= -MIN_EDGE:
        # Блокируем если свечной скор сильно против шорта
        if cs_short < -15:
            return "", f"candle_blocks_short(cs={cs_short})"
        reason = f"bear({bear_score}) vs bull({bull_score}): {','.join(signals[:4])}"
        return "short", reason
    else:
        return "", f"no_edge(bull={bull_score},bear={bear_score})"


async def check_entry(
    features: dict,
    forecast: dict,
    params: dict,
    setup_type: str = "normal",
    market_mode: str = "sideways",
) -> tuple[bool, str, str]:
    """Новая версия: только минимальные фильтры + скоринг"""
    if not forecast:
        return False, "", "no_data"

    try:
        prob = float(forecast.get("direction_probability") or 0)
    except Exception:
        prob = 0.0

    # Направление определяем из совокупности сигналов
    direction, dir_reason = detect_direction(features, forecast)
    if not direction:
        return False, "", f"no_direction({dir_reason})"

    volume = float(features.get("volume_24h") or 0)
    volume_bucket = str(features.get("volume_bucket") or volume_to_bucket(volume))
    volatility_bucket = str(features.get("volatility_bucket") or "unknown")

    # Минимальные фильтры (безопасность)

    if volume < 500_000:
        return False, "", f"low_volume({volume:.0f})"

    if volume_bucket == "trash":
        return False, "", "trash_liquidity"

    if volatility_bucket == "extreme":
        return False, "", "extreme_volatility"

    sr_signal = str(features.get("sr_signal") or "neutral")
    r_1h = float(features.get("r_1h") or 0)

    # Основное решение через скоринг
    scoring_should, scoring_score, scoring_reason = await scoring.should_enter(
        features, forecast, market_mode, direction
    )
    if not scoring_should:
        return False, "", f"scoring_reject({scoring_reason})"

    # В боковике — только от S/R уровней
    if market_mode == "bear_sideways":
        if direction == "short" and sr_signal != "bounce_resistance":
            return False, "", f"sideways_needs_resistance(sr={sr_signal})"
        if direction == "long" and sr_signal not in ("bounce_support", "breakout_up"):
            return False, "", f"sideways_needs_support(sr={sr_signal})"

    # В bull — только лонги, шорты запрещены
    # Лонги только при r_1h <= 0.05% (не входим на разогнанных монетах)
    if market_mode == "bull":
        if direction == "short":
            return False, "", f"no_shorts_in_bull"
        if direction == "long" and r_1h > 0.05:
            return False, "", f"bull_long_needs_flat_r1h({r_1h:.3f}>0.05)"

    # В bull_sideways — только лонги от поддержки, шорты запрещены
    if market_mode == "bull_sideways":
        if direction == "short":
            return False, "", f"no_shorts_in_bull_sideways"
        if direction == "long" and sr_signal not in ("bounce_support", "breakout_up"):
            return False, "", f"bull_sideways_needs_support(sr={sr_signal})"


    # В сильном тренде — только импульсный вход
    if market_mode == "bear" and direction == "short" and r_1h > -0.3:
        return False, "", f"bear_needs_impulse(r_1h={r_1h:.3f})"
    if market_mode == "bull" and direction == "long" and r_1h < 0.3:
        return False, "", f"bull_needs_impulse(r_1h={r_1h:.3f})"

    return True, direction, f"scoring({scoring_score})"


async def can_reenter(symbol: str, direction: str, forecast: dict) -> tuple[bool, str]:
    fc_age = (datetime.now(timezone.utc) - forecast["created_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60
    if fc_age > 30:
        return False, f"stale_forecast({fc_age:.0f}min)"

    prob = float(forecast.get("direction_probability") or 50)

    last_closed = await db.fetchrow(
        """SELECT exit_price, pnl_usdt, close_reason, closed_at
           FROM crypto_demo_trades
           WHERE symbol=$1 AND status='closed'
           ORDER BY closed_at DESC LIMIT 1""",
        symbol
    )

    if last_closed:
        if last_closed["close_reason"] and "stop_loss" in str(last_closed["close_reason"]):
            if prob < 56:
                return False, f"post_sl_weak({prob:.0f}%<56%)"

        age_min = (datetime.now(timezone.utc) - last_closed["closed_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60
        if age_min < 5 and prob < 57:
            return False, f"too_soon({age_min:.0f}min,{prob:.0f}%<57%)"

    return True, "ok"


def set_cooldown(symbol: str):
    pass


async def load_params() -> dict:
    row = await db.fetchrow("SELECT * FROM crypto_strategy_params WHERE id='current'")
    return dict(row) if row else {}


async def get_account() -> dict | None:
    row = await db.fetchrow("SELECT * FROM crypto_demo_accounts WHERE is_active=true LIMIT 1")
    return dict(row) if row else None


async def get_open_trades(account_id: str) -> list:
    rows = await db.fetch(
        "SELECT * FROM crypto_demo_trades WHERE account_id=$1 AND status='open'",
        account_id
    )
    return [dict(r) for r in rows]


async def get_price(symbol: str) -> float | None:
    row = await db.fetchrow(
        "SELECT price FROM crypto_prices_bybit WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
        symbol
    )
    if not row:
        return None

    price = float(row["price"])
    if price <= 0:
        return None

    min_prices = {"BTC": 1000, "ETH": 100, "BNB": 10, "SOL": 1}
    min_p = min_prices.get(symbol, 0.000001)
    if price < min_p:
        logger.warning(f"Suspicious price {symbol}: ${price} < min ${min_p}")
        return None

    return price


async def get_sl_price(symbol: str, price: float, direction: str, market_mode: str = "") -> tuple[float, float]:
    MAX_SL_PCT = 15.0
    SR_BUFFER = 0.005

    # Минимальный стоп для bull_sideways — 3% чтобы пережить шум
    MIN_SL_PCT = 3.0 if market_mode == "bull_sideways" else 0.5

    try:
        f_row = await db.fetchrow(
            "SELECT support_1, resistance_1 FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
            symbol
        )
        if f_row:
            if direction == "short" and f_row["resistance_1"]:
                res = float(f_row["resistance_1"])
                sl_price = res * (1 + SR_BUFFER)
                sl_pct = (sl_price - price) / price * 100
                if 0.5 <= sl_pct <= MAX_SL_PCT:
                    # Если стоп от SR меньше минимума — расширяем
                    if sl_pct < MIN_SL_PCT:
                        sl_pct = MIN_SL_PCT
                        sl_price = price * (1 + sl_pct / 100)
                    return sl_price, round(sl_pct, 2)
            elif direction == "long" and f_row["support_1"]:
                sup = float(f_row["support_1"])
                sl_price = sup * (1 - SR_BUFFER)
                sl_pct = (price - sl_price) / price * 100
                if 0.5 <= sl_pct <= MAX_SL_PCT:
                    # Если стоп от SR меньше минимума — расширяем
                    if sl_pct < MIN_SL_PCT:
                        sl_pct = MIN_SL_PCT
                        sl_price = price * (1 - sl_pct / 100)
                    return sl_price, round(sl_pct, 2)
    except Exception:
        pass

    sl_pct = max(MIN_SL_PCT, MAX_SL_PCT) if market_mode == "bull_sideways" else MAX_SL_PCT
    if direction == "short":
        return price * (1 + sl_pct / 100), sl_pct
    return price * (1 - sl_pct / 100), sl_pct


async def get_atr_sl(symbol: str, price: float) -> float:
    _, sl_pct = await get_sl_price(symbol, price, "short")
    return sl_pct


async def open_trade(
    account,
    symbol,
    direction,
    price,
    params,
    forecast,
    features,
    sr_data=None,
    setup_type="normal",
    sl_price_override=None,
    market_mode="sideways",
):
    if not price or price <= 0:
        logger.warning(f"Invalid price {symbol}: {price}")
        return None

    balance = float(account["current_balance"])
    size = calc_position_size(
        balance,
        params,
        features,
        setup_type,
        float(forecast.get("direction_probability") or 0),
    )
    if size < 10:
        logger.info(f"SKIP {symbol}: position too small after liquidity sizing (${size:.2f})")
        return None

    crypto = size / price
    default_sl_price, _ = await get_sl_price(symbol, price, direction, market_mode=market_mode)
    sl_price = float(sl_price_override) if sl_price_override and sl_price_override > 0 else default_sl_price

    if direction == "short":
        sl_pct = (sl_price - price) / price * 100
    else:
        sl_pct = (price - sl_price) / price * 100

    sl_pct = round(sl_pct, 2)

    # Применяем минимальный стоп для bull_sideways (3%) даже если sr_sl_price был переопределён
    min_sl = 3.0 if market_mode == "bull_sideways" else 0.5
    if sl_pct < min_sl:
        sl_pct = min_sl
        if direction == "short":
            sl_price = price * (1 + sl_pct / 100)
        else:
            sl_price = price * (1 - sl_pct / 100)
    tp1 = float(params.get("tp1_percent") or 2.0)

    # Всегда используем актуальные features (содержат candle_score, FVG, MS и т.д.)
    entry_features = {
        "regime": features.get("regime"),
        "rsi_14": features.get("rsi_14"),
        "r_1h": features.get("r_1h"),
        "r_24h": features.get("r_24h"),
        "volume_24h": features.get("volume_24h"),
        "sr_signal": features.get("sr_signal"),
        "sr_strength": features.get("sr_strength"),
        "btc_regime": features.get("btc_regime"),
        "btc_structure_4h": features.get("btc_structure_4h"),
        "market_mode": features.get("market_mode"),
        "btc_momentum": features.get("btc_momentum"),
        "relative_strength": features.get("relative_strength"),
        "volume_bucket": features.get("volume_bucket"),
        "volatility_bucket": features.get("volatility_bucket"),
        "impulse_score": features.get("impulse_score"),
        "reversal_score": features.get("reversal_score"),
        # Свечной анализ
        "candlestick_pattern": features.get("candlestick_pattern"),
        "candlestick_score": features.get("candlestick_score"),
        "candle_score_long": features.get("candle_score_long"),
        "candle_score_short": features.get("candle_score_short"),
        # FVG
        "in_bullish_fvg": features.get("in_bullish_fvg"),
        "in_bearish_fvg": features.get("in_bearish_fvg"),
        "nearest_fvg": features.get("nearest_fvg"),
        # Order Blocks
        "in_bullish_ob": features.get("in_bullish_ob"),
        "in_bearish_ob": features.get("in_bearish_ob"),
        # Market Structure
        "ms_structure": features.get("ms_structure"),
        "ms_bos_bullish": features.get("ms_bos_bullish"),
        "ms_bos_bearish": features.get("ms_bos_bearish"),
        "ms_choch_bullish": features.get("ms_choch_bullish"),
        "ms_choch_bearish": features.get("ms_choch_bearish"),
        # SR уровни
        "support_1": features.get("support_1"),
        "resistance_1": features.get("resistance_1"),
        "distance_to_support_pct": features.get("distance_to_support_pct"),
        "distance_to_resistance_pct": features.get("distance_to_resistance_pct"),
    }

    row = await db.fetchrow(
        """INSERT INTO crypto_demo_trades
           (account_id,symbol,trade_type,amount_usdt,amount_crypto,entry_price,
            sl_price,
            status,leverage,peak_pnl_usdt,trough_pnl_usdt,
            forecast_id,forecast_direction,forecast_probability,features_snapshot,setup_type,mirrored_to_bybit)
           VALUES ($1,$2,$3,$4,$5,$6,$7,'open',1.0,0.0,0.0,$8,$9,$10,$11,$12,false)
           RETURNING *""",
        account["id"], symbol, direction,
        round(size, 2), crypto, price,
        sl_price,
        forecast.get("id"),
        forecast.get("direction"),
        forecast.get("direction_probability"),
        json.dumps(entry_features),
        normalize_setup_type(setup_type)
    )
    if not row:
        return None

    sr_nearest = sr_data.get("nearest_support") if sr_data and direction == "long" else (
        sr_data.get("nearest_resistance") if sr_data else None
    )

    if sr_nearest:
        sr_level = float(sr_nearest["price"])
        sr_conf = float(sr_nearest.get("confluence_score", 0))
        logger.info(
            f"OPEN {direction.upper()} {symbol} @ ${price:,.4f} size=${size:,.0f} "
            f"sl={sl_pct}% | SR level=${sr_level:,.4f} conf={sr_conf:.2f} | setup={setup_type}"
        )
    else:
        logger.info(
            f"OPEN {direction.upper()} {symbol} @ ${price:,.4f} size=${size:,.0f} "
            f"sl={sl_pct}% | setup={setup_type}"
        )

    btc_ctx = entry_features.get("btc_context") or {}

    reason_text = (
        f"\n\n📊 <b>Причина входа:</b>\n"
        f"🎯 Вероятность: {float(forecast.get('direction_probability') or 0):.1f}%\n"
        f"🧠 Сетап: {normalize_setup_type(setup_type)}\n"
        f"📉 Тренд: {entry_features.get('regime', '-')}\n"
        f"📊 RSI: {float(entry_features.get('rsi_14') or 0):.1f}\n"
        f"⚡ Импульс 1ч: {float(entry_features.get('r_1h') or 0):.3f}\n"
        f"🌊 Импульс 24ч: {float(entry_features.get('r_24h') or 0):.3f}\n"
        f"💰 Объем: {float(entry_features.get('volume_24h') or 0) / 1_000_000:.1f}M\n"
        f"🧱 SR сигнал: {entry_features.get('sr_signal', '-')}\n"
        f"📦 Liquidity bucket: {entry_features.get('volume_bucket', '-')}\n"
        f"⚠️ Volatility bucket: {entry_features.get('volatility_bucket', '-')}\n"
        f"💪 Relative strength: {float(entry_features.get('relative_strength') or 0):.2f}\n"
        f"₿ BTC: {btc_ctx.get('global_regime', entry_features.get('btc_regime', '-'))} / "
        f"{btc_ctx.get('price_structure_4h', entry_features.get('btc_structure_4h', '-'))}"
    )

    await tg.send(
        tg.fmt_open(symbol, direction, price, size, sl_pct, tp1) + reason_text,
        account.get("telegram_chat_id") or TELEGRAM_CHAT_ID
    )
    return dict(row)


async def check_exit(trade, price, params):
    if price <= 0:
        return False, "", 0

    entry = float(trade["entry_price"])
    size = float(trade["amount_usdt"])
    crypto = float(trade["amount_crypto"])
    direction = trade["trade_type"]
    peak = float(trade.get("peak_pnl_usdt") or 0)

    pnl = (price - entry) * crypto if direction == "long" else (entry - price) * crypto
    pnl_pct = pnl / size * 100

    if pnl > peak:
        await db.execute(
            "UPDATE crypto_demo_trades SET peak_pnl_usdt=$1 WHERE id=$2",
            pnl,
            trade["id"]
        )
        peak = pnl

    peak_pct = peak / size * 100

    prev = trade.get("close_reason") or ""
    has_tp1 = "tp1" in prev
    fee_pct = float(params.get("fee_rate_taker") or 0.055) * 2 + 0.1
    sl_price = float(trade.get("sl_price") or 0)

    if sl_price > 0:
        if direction == "long" and price <= sl_price:
            return True, "stop_loss", 100
        if direction == "short" and price >= sl_price:
            return True, "stop_loss", 100


    latest_r_24h = None
    latest_market_mode = "sideways"

    sr_features = await db.fetchrow(
        """SELECT support_1, resistance_1, r_24h, atr, price, market_mode
           FROM crypto_features_hourly
           WHERE symbol=$1 ORDER BY ts DESC LIMIT 1""",
        trade["symbol"]
    )

    if sr_features and sr_features["r_24h"] is not None:
        latest_r_24h = float(sr_features["r_24h"])
    if sr_features and sr_features["market_mode"]:
        latest_market_mode = str(sr_features["market_mode"])

    scalp_tp = float(params.get("scalp_tp_percent") or 0.6)
    scalp_sl = float(params.get("scalp_sl_percent") or 0.7)

    if latest_market_mode == "bear_sideways":
        if pnl_pct >= scalp_tp:
            return True, f"scalp_tp({scalp_tp:.1f})", 100
        if pnl_pct <= -scalp_sl:
            return True, f"scalp_sl({scalp_sl:.1f})", 100

    latest_fc = await get_latest_forecast(trade["symbol"], "4h")
    if latest_fc:
        fc_dir = normalize_direction(latest_fc.get("direction"))
        fc_prob = float(latest_fc.get("direction_probability") or 0)

        if direction == "long" and fc_dir == "short" and fc_prob >= 80:
            return True, f"opposite_forecast_exit({fc_prob:.1f})", 100

        if direction == "short" and fc_dir == "long" and fc_prob >= 80:
            return True, f"opposite_forecast_exit({fc_prob:.1f})", 100

    if has_tp1 and params.get("be_stop_after_tp1", True) and pnl_pct <= fee_pct:
        return True, "breakeven_stop", 100

    if sr_features:
        if direction == "long" and sr_features["resistance_1"]:
            sr_tp = float(sr_features["resistance_1"])
            sr_tp_pct = (sr_tp - entry) / entry * 100
            if sr_tp_pct >= 0.5 and price >= sr_tp * 0.9965:
                return True, "tp_sr_resistance", 100

        elif direction == "short" and sr_features["support_1"]:
            sr_tp = float(sr_features["support_1"])
            sr_tp_pct = (entry - sr_tp) / entry * 100
            if sr_tp_pct >= 0.5 and price <= sr_tp * 1.0035:
                return True, "tp_sr_support", 100

    trail_start = float(params.get("trail_start_percent") or 2.5)
    if peak_pct >= trail_start:
        offset = 1.8
        if sr_features and sr_features["atr"] and sr_features["price"]:
            atr_pct = float(sr_features["atr"]) / float(sr_features["price"]) * 100
            offset = max(
                1.2,
                min(4.5, atr_pct * float(params.get("runner_trail_atr_mult") or 1.8))
            )

        if pnl_pct <= peak_pct - offset:
            return True, "trailing_stop", 100

    if peak_pct >= 5.0:
        floor = max(1.0, peak_pct * 0.35)
        if pnl_pct <= floor:
            return True, "trailing_breakeven", 100

    if peak_pct >= 5.0 and not has_tp1 and pnl_pct <= peak_pct - 2.5:
        return True, "peak_protection", 100

    # мягкий защитный выход при развороте higher timeframe
    if latest_r_24h is not None:
        if direction == "long" and latest_r_24h < -5 and pnl_pct > -2:
            return True, "htf_momentum_flip", 100
        if direction == "short" and latest_r_24h > 5 and pnl_pct > -2:
            return True, "htf_momentum_flip", 100

    return False, "", 0


async def close_trade(trade, price, reason, close_pct, account, params):
    entry = float(trade["entry_price"])
    size = float(trade["amount_usdt"])
    crypto = float(trade["amount_crypto"])
    direction = trade["trade_type"]
    fee = float(params.get("fee_rate_taker") or 0.055)
    prev = trade.get("close_reason") or ""

    slippage_pct = float(params.get("slippage_percent") or 0.15) / 100.0

    exec_price = price
    if direction == "long":
        exec_price = price * (1 - slippage_pct)
    else:
        exec_price = price * (1 + slippage_pct)

    if close_pct < 100:
        frac = close_pct / 100
        closed_crypto = crypto * frac
        closed_usdt = size * frac

        gross = (
            (exec_price - entry) * closed_crypto
            if direction == "long"
            else (entry - exec_price) * closed_crypto
        )
        fees = closed_usdt * (2 * fee / 100) + closed_usdt * 0.001
        pnl = gross - fees
        new_reason = f"{prev},{reason}" if prev else reason

        await db.execute(
            "UPDATE crypto_demo_trades SET amount_usdt=$1, amount_crypto=$2, close_reason=$3 WHERE id=$4",
            size - closed_usdt,
            crypto - closed_crypto,
            new_reason,
            trade["id"]
        )

        await db.execute(
            "UPDATE crypto_demo_accounts SET current_balance=current_balance+$1 WHERE id=$2",
            pnl,
            account["id"]
        )

        await tg.send(
            tg.fmt_partial(
                trade["symbol"],
                int(close_pct),
                pnl,
                size - closed_usdt,
                reason
            ),
            account.get("telegram_chat_id") or TELEGRAM_CHAT_ID
        )

        logger.info(
            f"PARTIAL {trade['symbol']} {close_pct}% @ ${exec_price:,.6f} "
            f"pnl=${pnl:,.2f} [{reason}]"
        )
        return pnl

    gross = (
        (exec_price - entry) * crypto
        if direction == "long"
        else (entry - exec_price) * crypto
    )
    fees = size * (2 * fee / 100) + size * 0.001
    pnl = gross - fees
    pnl_pct = pnl / size * 100
    hold_h = (datetime.now(timezone.utc) - trade["opened_at"].replace(tzinfo=timezone.utc)).total_seconds() / 3600
    full_reason = f"{prev},{reason}" if prev else reason

    await db.execute(
        "UPDATE crypto_demo_trades SET exit_price=$1, pnl_usdt=$2, status='closed', closed_at=now(), close_reason=$3 WHERE id=$4",
        exec_price,
        pnl,
        full_reason,
        trade["id"]
    )

    await db.execute(
        "UPDATE crypto_demo_accounts SET current_balance=current_balance+$1 WHERE id=$2",
        pnl,
        account["id"]
    )

    fresh_account = await db.fetchrow(
        "SELECT current_balance FROM crypto_demo_accounts WHERE id=$1",
        account["id"]
    )
    new_bal = float(fresh_account["current_balance"]) if fresh_account else float(account["current_balance"]) + pnl

    await tg.send(
        tg.fmt_close(
            trade["symbol"],
            direction,
            entry,
            exec_price,
            pnl,
            pnl_pct,
            reason,
            hold_h,
            new_bal
        ),
        account.get("telegram_chat_id") or TELEGRAM_CHAT_ID
    )

    e = "✅" if pnl > 0 else "❌"
    logger.info(
        f"{e} CLOSE {trade['symbol']} {direction.upper()} @ ${exec_price:,.6f} "
        f"pnl=${pnl:,.2f} ({pnl_pct:.2f}%) [{reason}]"
    )
    return pnl


async def fast_exit_check(prices: dict):
    params = await load_params()
    account = await get_account()
    if not account or params.get("kill_switch_active"):
        return

    open_trades = await get_open_trades(account["id"])
    for trade in open_trades:
        price = prices.get(trade["symbol"])
        if not price or price <= 0:
            continue
        should, reason, pct = await check_exit(trade, price, params)
        if should:
            pnl = await close_trade(trade, price, reason, pct, account, params)
            if pnl is not None and pnl < 0:
                set_cooldown(trade["symbol"])
            account = await get_account()


async def trading_cycle():
    logger.info("Cycle start")
    params = await load_params()
    logger.info("Cycle params loaded")
    account = await get_account()
    logger.info("Cycle account loaded")
    if not account:
        logger.info("Cycle stopped: no account")
        return

    banned = set(params.get("banned_symbols") or [])
    fc_max_age = float(params.get("forecast_max_age_minutes") or 30)

    open_trades = await get_open_trades(account["id"])
    logger.info(f"Cycle open trades loaded: {len(open_trades)}")
    for trade in open_trades:
        price = await get_price(trade["symbol"])
        if not price:
            continue
        should, reason, pct = await check_exit(trade, price, params)
        if should:
            pnl = await close_trade(trade, price, reason, pct, account, params)
            if pnl is not None and pnl < 0:
                set_cooldown(trade["symbol"])
            account = await get_account()

    if params.get("kill_switch_active"):
        logger.info("Cycle stopped: kill switch active")
        return

    open_trades = await get_open_trades(account["id"])
    logger.info(f"Cycle open trades reloaded: {len(open_trades)}")
    open_syms = {t["symbol"] for t in open_trades}

    if len(open_trades) >= MAX_OPEN:
        logger.info(f"Cycle stopped: max open reached ({len(open_trades)}/{MAX_OPEN})")
        return

    balance = float(account["current_balance"])
    initial = float(account["initial_balance"])
    dd_pct = (initial - balance) / initial * 100 if initial > 0 else 0
    logger.info(f"Drawdown: {dd_pct:.1f}% | balance=${balance:,.0f}")

    logger.info("Cycle before fear&greed fetch")
    fg_row = await db.fetchrow("SELECT value FROM crypto_fear_greed WHERE id='latest'")
    fg = float(fg_row["value"]) if fg_row else 50.0
    allowed = get_allowed_direction(fg)
    logger.info(f"Cycle fear&greed loaded: {fg:.0f} -> {allowed}")
    logger.info(f"Cycle: FG={fg:.0f} → {allowed} | open={len(open_trades)}/{MAX_OPEN}")

    symbols = await db.fetch("SELECT symbol FROM crypto_assets WHERE is_active=true ORDER BY rank")
    logger.info(f"Cycle symbols loaded: {len(symbols)}")
    candidates = [
        r["symbol"] for r in symbols
        if r["symbol"] not in open_syms and r["symbol"] not in banned
    ]
    logger.info(f"Cycle candidates prepared: {len(candidates)}")

    btc_mode_row = await db.fetchrow(
        "SELECT market_mode FROM crypto_features_hourly WHERE symbol='BTC' ORDER BY ts DESC LIMIT 1"
    )
    current_market_mode = str(btc_mode_row["market_mode"]) if btc_mode_row and btc_mode_row["market_mode"] else "sideways"
    new_per_cycle_limit = SCALP_MAX_NEW_PER_CYCLE if current_market_mode == "bear_sideways" else MAX_NEW_PER_CYCLE

    new_trades = 0

    async with aiohttp.ClientSession() as sr_session:
        for symbol in candidates:
            if new_trades >= new_per_cycle_limit:
                break

            f_row = await db.fetchrow(
                "SELECT * FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
                symbol
            )
            if not f_row:
                continue

            features = dict(f_row)
            # Убедимся что все значения простые типы (не Decimal/UUID)
            features = {k: (float(v) if hasattr(v, '__float__') and not isinstance(v, bool) and not isinstance(v, str) else v) 
                       for k, v in features.items() 
                       if not callable(v)}

            age = (datetime.now(timezone.utc) - features["ts"].replace(tzinfo=timezone.utc)).total_seconds() / 60
            if age > 30:
                continue

            forecast = await get_latest_forecast(symbol, "4h")
            if not forecast:
                continue

            fc_age = (datetime.now(timezone.utc) - forecast["created_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60
            if fc_age > fc_max_age:
                continue

            market_mode = detect_market_mode(features, forecast)
            setup_type = detect_setup_type(features, forecast)
            should, direction, reason = await check_entry(features, forecast, params, setup_type, market_mode)

            if not should:
                if reason not in ("neutral_forecast", "no_data"):
                    logger.info(f"SKIP {symbol}: {reason}")
                continue

            forecast_snapshot = forecast.get("features_snapshot") or {}
            if isinstance(forecast_snapshot, str):
                try:
                    forecast_snapshot = json.loads(forecast_snapshot)
                except Exception:
                    forecast_snapshot = {}

            btc_momentum = (
                features.get("btc_momentum")
                or forecast_snapshot.get("btc_momentum")
                or "flat"
            )

            btc_ok, btc_reason = btc_move_allows_entry(
                setup_type,
                market_mode,
                btc_momentum,
                float(forecast.get("direction_probability") or 0),
                float(features.get("relative_strength") or 0)
            )

            if not btc_ok:
                logger.info(f"BLOCKED {symbol}: {btc_reason}")
                continue

            if allowed == "long_only" and direction != "long":
                logger.info(f"BLOCKED {symbol}: FG long_only")
                continue
            if allowed == "short_only" and direction != "short":
                logger.info(f"BLOCKED {symbol}: FG short_only")
                continue

            can_open, reentry_reason = await can_reenter(symbol, direction, forecast)
            if not can_open:
                logger.info(f"BLOCKED {symbol}: {reentry_reason}")
                continue

            sector = SECTOR.get(symbol, "other")
            sector_trades = [t for t in open_trades if SECTOR.get(t["symbol"], "other") == sector]
            if any(t["trade_type"] != direction for t in sector_trades):
                continue
            if len(sector_trades) >= int(params.get("max_positions_high_corr") or 3):
                continue

            # Считаем размер с учётом ликвидности
            planned_size = calc_position_size(
                balance,
                params,
                features,
                setup_type,
                float(forecast.get("direction_probability") or 0),
            )
            if planned_size < 10:
                logger.info(f"SKIP {symbol}: planned size too small (${planned_size:.2f})")
                continue

            exposure = sum(float(t["amount_usdt"]) for t in open_trades)
            max_exp = balance * float(params.get("max_total_exposure") or 25) / 100
            if exposure + planned_size > max_exp:
                logger.info("Cycle stopped: max exposure reached")
                break

            price = await get_price(symbol)
            if not price:
                continue

            sr_data = None
            sr_sl_price = None
            try:
                sr_data = await analyze_sr(sr_session, symbol)
                if sr_data:
                    await update_features_sr(symbol, sr_data)
            except Exception as e:
                logger.debug(f"SR analysis error {symbol}: {e}")

            sr_ok, sr_sl_price, sr_reason = get_sr_entry_signal(sr_data, direction)
            if sr_data and not sr_ok:
                logger.info(f"SR blocked {symbol} {direction}: {sr_reason}")
                continue

            logger.info(f"SETUP {symbol}: mode={market_mode} setup={setup_type} reason={reason}")

            trade = await open_trade(
                account,
                symbol,
                direction,
                price,
                params,
                forecast,
                features,
                sr_data,
                setup_type,
                sr_sl_price,
                market_mode=market_mode,
            )
            if trade:
                new_trades += 1
                open_trades.append(trade)
                open_syms.add(symbol)
                account = await get_account()
                balance = float(account["current_balance"])

    logger.info(f"Cycle finished: opened {new_trades} new trades")


async def run_trader():
    logger.info("Trader started")

    account = await get_account()
    if not account:
        count = await db.fetchval("SELECT COUNT(*) FROM crypto_demo_accounts")
        if count == 0:
            await db.execute(
                "INSERT INTO crypto_demo_accounts (initial_balance,current_balance,telegram_chat_id,is_active) VALUES ($1,$1,$2,true)",
                DEMO_INITIAL_BALANCE,
                TELEGRAM_CHAT_ID or ""
            )
            logger.info(f"Demo account created: ${DEMO_INITIAL_BALANCE:,.0f}")
        else:
            await db.execute(
                "UPDATE crypto_demo_accounts SET is_active=true "
                "WHERE id=(SELECT id FROM crypto_demo_accounts ORDER BY created_at LIMIT 1)"
            )
            logger.info("Reactivated existing account")

        account = await get_account()

    if account:
        bal = float(account["current_balance"])
        init = float(account["initial_balance"])
        pnl = bal - init
        pnl_pct = pnl / init * 100
        sign = "+" if pnl >= 0 else ""

        fg_row = await db.fetchrow("SELECT value, label FROM crypto_fear_greed WHERE id='latest'")
        fg_val = float(fg_row["value"]) if fg_row else 0
        fg_label = fg_row["label"] if fg_row else "Unknown"

        direction_mode = get_allowed_direction(fg_val)
        dir_emoji = "🟢" if direction_mode == "long_only" else "🔴" if direction_mode == "short_only" else "🟡"

        open_cnt = await db.fetchval("SELECT COUNT(*) FROM crypto_demo_trades WHERE status='open'")

        await tg.send(
            f"🤖 <b>Криптобот v1.1 перезапущен</b>\n\n"
            f"💰 Баланс: ${bal:,.0f}\n"
            f"📈 PnL: {sign}${pnl:,.0f} ({sign}{pnl_pct:.2f}%)\n"
            f"📊 Открытых позиций: {open_cnt}\n"
            f"😰 Fear & Greed: {fg_val:.0f} ({fg_label})\n"
            f"{dir_emoji} Режим: {direction_mode}",
            account.get("telegram_chat_id") or TELEGRAM_CHAT_ID
        )
        logger.info("Trader init message sent")

    logger.info("Trader warmup started")
    await asyncio.sleep(10)
    logger.info("Trader entering main loop")

    while True:
        try:
            await trading_cycle()
        except Exception as e:
            logger.error(f"Trading cycle error: {e}")
        await asyncio.sleep(TRADING_INTERVAL)

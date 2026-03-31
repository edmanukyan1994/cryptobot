import asyncio
import aiohttp
import logging
import numpy as np
from datetime import datetime, timezone

from config import bybit_symbol, FEATURE_INTERVAL
import db
from market_context import get_context

logger = logging.getLogger("features")
BYBIT_BASE = "https://api.bybit.com"

# ============================================================
# ТЕХНИЧЕСКИЕ ИНДИКАТОРЫ
# ============================================================

def calc_rsi(prices: list, period: int = 14):
    if len(prices) < period + 1:
        return None
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def calc_ema(prices: list, period: int):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = float(np.mean(prices[:period]))
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def calc_macd(prices: list):
    if len(prices) < 35:
        return None, None, None
    ema12 = calc_ema(prices, 12)
    ema26 = calc_ema(prices, 26)
    if ema12 is None or ema26 is None:
        return None, None, None
    macd_line = ema12 - ema26
    macd_vals = []
    for i in range(26, len(prices) + 1):
        e12 = calc_ema(prices[:i], 12)
        e26 = calc_ema(prices[:i], 26)
        if e12 is not None and e26 is not None:
            macd_vals.append(e12 - e26)
    signal = calc_ema(macd_vals, 9) if len(macd_vals) >= 9 else None
    hist = round(macd_line - signal, 6) if signal is not None else None
    return round(macd_line, 6), round(signal, 6) if signal is not None else None, hist


def calc_bollinger(prices: list, period: int = 20):
    if len(prices) < period:
        return None, None, None, None
    recent = prices[-period:]
    mid = float(np.mean(recent))
    std = float(np.std(recent))
    upper = mid + 2 * std
    lower = mid - 2 * std
    width = ((upper - lower) / mid * 100) if mid > 0 else 0
    return round(upper, 6), round(mid, 6), round(lower, 6), round(width, 4)


def calc_atr(prices: list, period: int = 14):
    if len(prices) < period + 1:
        return None
    trs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    return round(float(np.mean(trs[-period:])), 8)


def detect_regime(rsi, r_24h, fear_greed):
    fg = fear_greed or 50
    if fg <= 15:
        return "crash"
    if fg <= 25:
        return "bearish"
    if fg >= 80:
        return "euphoria"
    if r_24h > 3:
        return "bullish"
    if r_24h < -3:
        return "bearish"
    if abs(r_24h) < 1.0:
        return "consolidation"
    return "neutral"


def calc_risk_score(rsi, fear_greed, atr_pct, r_24h):
    score = 50.0
    fg = fear_greed or 50
    if fg < 20:
        score += 20
    elif fg > 80:
        score += 10
    if atr_pct > 5:
        score += 20
    elif atr_pct > 2:
        score += 10
    if rsi and (rsi < 25 or rsi > 75):
        score += 10
    if abs(r_24h) > 10:
        score += 10
    return min(100, round(score, 1))


# ============================================================
# ДОПОЛНИТЕЛЬНЫЕ КЛАССИФИКАТОРЫ
# ============================================================

def classify_volume_bucket(volume_24h: float) -> str:
    if volume_24h is None:
        return "unknown"
    if volume_24h >= 500_000_000:
        return "ultra"
    if volume_24h >= 100_000_000:
        return "high"
    if volume_24h >= 10_000_000:
        return "medium"
    if volume_24h >= 1_000_000:
        return "low"
    return "trash"


def classify_volatility_bucket(atr_pct: float) -> str:
    if atr_pct is None:
        return "unknown"
    if atr_pct >= 8:
        return "extreme"
    if atr_pct >= 4:
        return "high"
    if atr_pct >= 2:
        return "medium"
    return "low"


def calc_relative_strength(symbol_r_24h: float, btc_24h_change: float) -> float:
    try:
        return round(float(symbol_r_24h or 0) - float(btc_24h_change or 0), 4)
    except Exception:
        return 0.0


def calc_distance_pct(current: float, level: float | None) -> float | None:
    if not current or current <= 0 or not level or level <= 0:
        return None
    return round(abs(current - level) / current * 100, 4)


def calc_impulse_score(
    r_1h: float,
    r_24h: float,
    volume_bucket: str,
    sr_signal: str,
    relative_strength: float,
    btc_momentum: str,
):
    score = 0

    if abs(r_1h) >= 0.03:
        score += 1
    if abs(r_1h) >= 0.08:
        score += 1

    if volume_bucket in ("ultra", "high"):
        score += 1

    if sr_signal in ("breakout_up", "breakout_down"):
        score += 1

    if relative_strength >= 1.5 or relative_strength <= -1.5:
        score += 1

    if btc_momentum in ("strong_up", "strong_down"):
        score += 1

    return score


def calc_reversal_score(
    rsi: float | None,
    r_1h: float,
    r_24h: float,
    sr_signal: str,
    candlestick_pattern: str,
):
    score = 0

    if rsi is not None and (rsi <= 30 or rsi >= 70):
        score += 1
    if rsi is not None and (rsi <= 20 or rsi >= 80):
        score += 1

    # Divergence-like conflict
    if (r_1h > 0 and r_24h < 0) or (r_1h < 0 and r_24h > 0):
        score += 1

    if sr_signal in ("bounce_support", "bounce_resistance"):
        score += 1

    if candlestick_pattern in ("hammer", "shooting_star", "bullish_engulfing", "bearish_engulfing"):
        score += 1

    return score


# ============================================================
# S/R LEGACY HELPER
# ============================================================

def find_sr_proper(candles: list, current_price: float) -> tuple:
    """
    Legacy helper. Оставлен для совместимости.
    """
    if len(candles) < 20 or current_price <= 0:
        return None, None, "neutral", 0.0

    min_dist = current_price * 0.003  # минимум 0.3% от цены

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]
    avg_vol = np.mean(volumes) if volumes else 1

    lookback = min(10, len(candles) // 4)

    raw_supports = []
    raw_resistances = []

    for i in range(lookback, len(candles) - lookback):
        if (
            all(lows[i] <= lows[i - j] for j in range(1, lookback + 1))
            and all(lows[i] <= lows[i + j] for j in range(1, lookback + 1))
        ):
            touches = sum(
                1
                for j in range(max(0, i - lookback * 3), min(len(candles), i + lookback * 3))
                if abs(lows[j] - lows[i]) / lows[i] < 0.005
            )
            vol_weight = volumes[i] / avg_vol
            raw_supports.append(
                {
                    "price": lows[i],
                    "strength": touches + vol_weight,
                    "touches": touches,
                    "idx": i,
                }
            )

        if (
            all(highs[i] >= highs[i - j] for j in range(1, lookback + 1))
            and all(highs[i] >= highs[i + j] for j in range(1, lookback + 1))
        ):
            touches = sum(
                1
                for j in range(max(0, i - lookback * 3), min(len(candles), i + lookback * 3))
                if abs(highs[j] - highs[i]) / highs[i] < 0.005
            )
            vol_weight = volumes[i] / avg_vol
            raw_resistances.append(
                {
                    "price": highs[i],
                    "strength": touches + vol_weight,
                    "touches": touches,
                    "idx": i,
                }
            )

    def cluster_levels(levels, tolerance=0.005):
        if not levels:
            return []

        levels = sorted(levels, key=lambda x: x["price"])
        clusters = []
        cur = [levels[0]]

        for lv in levels[1:]:
            if abs(lv["price"] - cur[-1]["price"]) / cur[-1]["price"] <= tolerance:
                cur.append(lv)
            else:
                clusters.append(cur)
                cur = [lv]
        clusters.append(cur)

        result = []
        for cl in clusters:
            avg_price = np.mean([l["price"] for l in cl])
            total_str = sum(l["strength"] for l in cl)
            max_touches = max(l["touches"] for l in cl)
            result.append(
                {
                    "price": round(avg_price, 8),
                    "strength": round(total_str, 2),
                    "touches": max_touches,
                    "zone_size": len(cl),
                }
            )
        return sorted(result, key=lambda x: -x["strength"])

    all_supports = cluster_levels(raw_supports)
    all_resistances = cluster_levels(raw_resistances)

    supports = [s for s in all_supports if current_price - s["price"] >= min_dist]
    resistances = [r for r in all_resistances if r["price"] - current_price >= min_dist]

    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None

    signal = "neutral"
    strength = 0.0

    if nearest_support and nearest_resistance:
        dist_sup = (current_price - nearest_support["price"]) / current_price * 100
        dist_res = (nearest_resistance["price"] - current_price) / current_price * 100

        if dist_sup <= 1.5:
            signal = "bounce_support"
            strength = min(100, nearest_support["strength"] * 10 * (1 - dist_sup / 3))
        elif dist_res <= 1.5:
            signal = "bounce_resistance"
            strength = min(100, nearest_resistance["strength"] * 10 * (1 - dist_res / 3))
        elif current_price > nearest_resistance["price"]:
            signal = "breakout_up"
            strength = min(100, nearest_resistance["strength"] * 8)
        elif current_price < nearest_support["price"]:
            signal = "breakout_down"
            strength = min(100, nearest_support["strength"] * 8)

    sup_price = round(nearest_support["price"], 8) if nearest_support else None
    res_price = round(nearest_resistance["price"], 8) if nearest_resistance else None

    return sup_price, res_price, signal, round(strength, 1)


# ============================================================
# КАНДЛСТИК ПАТТЕРНЫ
# ============================================================

def detect_candle(candles: list):
    if len(candles) < 3:
        return "none", 0.0

    c = candles[-1]
    o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
    body = abs(cl - o)
    rng = h - l

    if rng == 0:
        return "none", 0.0

    body_pct = body / rng
    if body_pct < 0.1:
        return "doji", 0.0

    lower_shadow = min(o, cl) - l
    upper_shadow = h - max(o, cl)

    if lower_shadow > body * 2 and upper_shadow < body * 0.5 and cl > o:
        return "hammer", 0.8
    if upper_shadow > body * 2 and lower_shadow < body * 0.5 and cl < o:
        return "shooting_star", -0.8

    prev = candles[-2]
    po, pc = float(prev["open"]), float(prev["close"])

    if cl < o and pc > po and o > pc and cl < po:
        return "bearish_engulfing", -0.9
    if cl > o and pc < po and o < pc and cl > po:
        return "bullish_engulfing", 0.9
    if cl > o and body_pct > 0.6:
        return "bullish_marubozu", 0.5
    if cl < o and body_pct > 0.6:
        return "bearish_marubozu", -0.5

    return "none", 0.0


# ============================================================
# KLINES
# ============================================================

async def fetch_klines(session, sym: str, interval="60", limit=200):
    try:
        bs = bybit_symbol(sym)
        async with session.get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "linear", "symbol": bs, "interval": interval, "limit": limit},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("retCode") != 0:
                return []
            return [
                {
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                }
                for r in reversed(data["result"]["list"])
            ]
    except Exception as e:
        logger.debug(f"fetch_klines error {sym}: {e}")
        return []


# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================

async def build_features(session, symbol: str):
    price_rows = await db.fetch(
        "SELECT price FROM crypto_prices_bybit WHERE symbol=$1 ORDER BY ts DESC LIMIT 60",
        symbol
    )
    if len(price_rows) < 5:
        return None

    prices = [float(r["price"]) for r in reversed(price_rows)]
    current = prices[-1]

    latest = await db.fetchrow(
        "SELECT volume_24h, price_change_24h FROM crypto_prices_bybit WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
        symbol
    )
    if not latest:
        return None

    fg_row = await db.fetchrow("SELECT value FROM crypto_fear_greed WHERE id='latest'")
    fg = float(fg_row["value"]) if fg_row else 50.0

    global_row = await db.fetchrow("SELECT btc_dominance FROM crypto_market_global WHERE id='latest'")
    btc_dom = float(global_row["btc_dominance"]) if global_row else 55.0

    market_ctx = get_context()
    btc_regime = str(market_ctx.get("global_regime") or "neutral")
    btc_structure_4h = str(market_ctx.get("price_structure_4h") or "sideways")
    market_mode = str(market_ctx.get("market_mode") or "sideways")
    btc_momentum = str(market_ctx.get("momentum") or "flat")
    btc_24h_change = float(market_ctx.get("btc_24h_change") or 0.0)
    is_aggressive_bear = bool(market_ctx.get("is_aggressive_bear") or False)
    is_aggressive_bull = bool(market_ctx.get("is_aggressive_bull") or False)
    no_long_zone = bool(market_ctx.get("no_long_zone") or False)
    no_short_zone = bool(market_ctx.get("no_short_zone") or False)
    btc_move_strength = float(market_ctx.get("btc_move_strength") or 0.0)

    klines = await fetch_klines(session, symbol, interval="60", limit=200)

    rsi = calc_rsi(prices)
    macd, macd_sig, macd_hist = calc_macd(prices)
    bb_u, bb_m, bb_l, bb_w = calc_bollinger(prices)
    atr = calc_atr(prices)
    atr_pct = (atr / current * 100) if atr and current > 0 else 0

    r_24h = float(latest["price_change_24h"] or 0)
    r_1h = round((prices[-1] - prices[-2]) / prices[-2] * 100, 4) if len(prices) >= 2 and prices[-2] > 0 else 0

    candle_pattern, candle_score = detect_candle(klines) if klines else ("none", 0.0)

    # S/R анализ через текущий sr_engine
    if klines and len(klines) >= 20:
        try:
            from sr_engine import (
                find_horizontal_levels,
                find_psychological_levels,
                find_ma_levels,
                find_fibonacci_levels,
                find_pivot_points,
                find_volume_profile,
                calc_confluence,
            )

            all_levels = []
            for fn, args in [
                (find_horizontal_levels, (klines, current)),
                (find_psychological_levels, (current,)),
                (find_ma_levels, (klines, current)),
                (find_fibonacci_levels, (klines, current)),
                (find_pivot_points, (klines, current)),
                (find_volume_profile, (klines, current)),
            ]:
                r = fn(*args)
                all_levels += r.get("supports", []) + r.get("resistances", [])

            sr_result = calc_confluence(all_levels, current)
            sup = sr_result["nearest_support"]["price"] if sr_result["nearest_support"] else None
            res = sr_result["nearest_resistance"]["price"] if sr_result["nearest_resistance"] else None
            sr_sig = sr_result["signal"]
            sr_str = sr_result["signal_strength"]
        except Exception as e:
            logger.warning(f"SR engine error {symbol}: {e}")
            sup, res, sr_sig, sr_str = find_sr_proper(klines, current)
    else:
        sup, res, sr_sig, sr_str = None, None, "neutral", 0.0

    dist_to_support_pct = calc_distance_pct(current, sup)
    dist_to_resistance_pct = calc_distance_pct(current, res)

    volume_24h = float(latest["volume_24h"] or 0)
    volume_bucket = classify_volume_bucket(volume_24h)
    volatility_bucket = classify_volatility_bucket(atr_pct)
    relative_strength = calc_relative_strength(r_24h, btc_24h_change)

    impulse_score = calc_impulse_score(
        r_1h=r_1h,
        r_24h=r_24h,
        volume_bucket=volume_bucket,
        sr_signal=sr_sig,
        relative_strength=relative_strength,
        btc_momentum=btc_momentum,
    )

    reversal_score = calc_reversal_score(
        rsi=rsi,
        r_1h=r_1h,
        r_24h=r_24h,
        sr_signal=sr_sig,
        candlestick_pattern=candle_pattern,
    )

    regime = detect_regime(rsi, r_24h, fg)
    risk_score = calc_risk_score(rsi, fg, atr_pct, r_24h)

    return {
        "symbol": symbol,
        "ts": datetime.now(timezone.utc),

        "price": current,
        "volume_24h": volume_24h,
        "r_1h": r_1h,
        "r_24h": r_24h,

        "rsi_14": rsi,
        "macd": macd,
        "macd_signal": macd_sig,
        "macd_histogram": macd_hist,

        "bollinger_upper": bb_u,
        "bollinger_middle": bb_m,
        "bollinger_lower": bb_l,
        "bollinger_width": bb_w,

        "atr": atr,
        "fear_greed_index": fg,
        "btc_dominance": btc_dom,

        "regime": regime,
        "risk_score": risk_score,

        "candlestick_pattern": candle_pattern,
        "candlestick_score": candle_score,

        "support_1": sup,
        "resistance_1": res,
        "sr_signal": sr_sig,
        "sr_strength": sr_str,
        
        "is_aggressive_bear": is_aggressive_bear,
        "is_aggressive_bull": is_aggressive_bull,
        "no_long_zone": no_long_zone,
        "no_short_zone": no_short_zone,
        "btc_move_strength": btc_move_strength,

        # новые поля
        "btc_regime": btc_regime,
        "btc_structure_4h": btc_structure_4h,
        "market_mode": market_mode,
        "btc_momentum": btc_momentum,
        "relative_strength": relative_strength,
        "volume_bucket": volume_bucket,
        "volatility_bucket": volatility_bucket,
        "impulse_score": impulse_score,
        "reversal_score": reversal_score,
        "distance_to_support_pct": dist_to_support_pct,
        "distance_to_resistance_pct": dist_to_resistance_pct,
    }


async def run_features_builder():
    logger.info("Features builder started")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                symbols = await db.fetch("SELECT symbol FROM crypto_assets WHERE is_active=true ORDER BY rank")
                built = 0

                for row in symbols:
                    sym = row["symbol"]
                    try:
                        f = await build_features(session, sym)
                        if f:
                            await db.execute(
                                """
                                INSERT INTO crypto_features_hourly
                                (
                                    symbol, ts, price, volume_24h, r_1h, r_24h, rsi_14,
                                    macd, macd_signal, macd_histogram,
                                    bollinger_upper, bollinger_middle, bollinger_lower, bollinger_width,
                                    atr, fear_greed_index, btc_dominance, regime, risk_score,
                                    candlestick_pattern, candlestick_score,
                                    support_1, resistance_1, sr_signal, sr_strength,
                                    btc_regime, btc_structure_4h, market_mode, btc_momentum,
                                    relative_strength, volume_bucket, volatility_bucket,
                                    impulse_score, reversal_score,
                                    distance_to_support_pct, distance_to_resistance_pct,
                                    is_aggressive_bear, is_aggressive_bull, no_long_zone, no_short_zone, btc_move_strength
                                )
                                VALUES
                                (
                                    $1,$2,$3,$4,$5,$6,$7,
                                    $8,$9,$10,
                                    $11,$12,$13,$14,
                                    $15,$16,$17,$18,$19,
                                    $20,$21,
                                    $22,$23,$24,$25,
                                    $26,$27,$28,$29,
                                    $30,$31,$32,
                                    $33,$34,
                                    $35,$36,$37,$38,$39,$40,$41
                                )
                                """,
                                f["symbol"], f["ts"], f["price"], f["volume_24h"], f["r_1h"], f["r_24h"], f["rsi_14"],
                                f["macd"], f["macd_signal"], f["macd_histogram"],
                                f["bollinger_upper"], f["bollinger_middle"], f["bollinger_lower"], f["bollinger_width"],
                                f["atr"], f["fear_greed_index"], f["btc_dominance"], f["regime"], f["risk_score"],
                                f["candlestick_pattern"], f["candlestick_score"],
                                f["support_1"], f["resistance_1"], f["sr_signal"], f["sr_strength"],
                                f["btc_regime"], f["btc_structure_4h"], f["market_mode"], f["btc_momentum"],
                                f["relative_strength"], f["volume_bucket"], f["volatility_bucket"],
                                f["impulse_score"], f["reversal_score"],
                                f["distance_to_support_pct"], f["distance_to_resistance_pct"],
                                f["is_aggressive_bear"], f["is_aggressive_bull"],
                                f["no_long_zone"], f["no_short_zone"],
                                 f["btc_move_strength"],
                            )
                            built += 1
                    except Exception as e:
                        logger.warning(f"Features error {sym}: {e}")

                    await asyncio.sleep(0.3)

                logger.info(f"Features built: {built}/{len(symbols)}")

            except Exception as e:
                logger.error(f"Features builder error: {e}")

            await asyncio.sleep(FEATURE_INTERVAL)

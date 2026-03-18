import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from loguru import logger
import db
import telegram_bot as tg
from config import settings
from forecaster import get_latest_forecast

# ============================================================
# КОНСТАНТЫ
# ============================================================
MAX_OPEN_TRADES = 10
MAX_NEW_TRADES_PER_CYCLE = 3
SECTOR_MAP = {
    "BTC": "btc", "ETH": "eth", "BNB": "exchange",
    "SOL": "alt-l1", "ADA": "alt-l1", "AVAX": "alt-l1", "DOT": "alt-l1",
    "XRP": "payments", "TRX": "payments", "LTC": "payments", "XLM": "payments",
    "DOGE": "meme", "SHIB": "meme", "PEPE": "meme", "BONK": "meme", "WIF": "meme",
    "LINK": "oracle", "UNI": "defi", "AAVE": "defi",
    "ARB": "l2", "OP": "l2",
    "NEAR": "alt-l1", "APT": "alt-l1", "SUI": "alt-l1",
    "ATOM": "alt-l1", "FIL": "storage",
}

# ============================================================
# ЗАГРУЗКА ПАРАМЕТРОВ
# ============================================================

async def load_params() -> dict:
    row = await db.fetchrow("SELECT * FROM crypto_strategy_params WHERE id='current'")
    if not row:
        logger.warning("No strategy params found, using defaults")
        return {}
    return dict(row)

async def get_account() -> dict | None:
    row = await db.fetchrow("SELECT * FROM crypto_demo_accounts WHERE is_active=true LIMIT 1")
    return dict(row) if row else None

async def get_open_trades(account_id: str) -> list[dict]:
    rows = await db.fetch(
        "SELECT * FROM crypto_demo_trades WHERE account_id=$1 AND status='open'",
        account_id
    )
    return [dict(r) for r in rows]

async def get_current_price(symbol: str) -> float | None:
    row = await db.fetchrow(
        "SELECT price FROM crypto_prices_bybit WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
        symbol
    )
    return float(row["price"]) if row else None

# ============================================================
# ATR-BASED STOP LOSS
# ============================================================

async def calc_atr_sl(symbol: str, current_price: float) -> float:
    """Вычисляет стоп-лосс в % на основе ATR."""
    row = await db.fetchrow(
        "SELECT atr FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
        symbol
    )
    if not row or not row["atr"]:
        return 2.5  # дефолт

    atr = float(row["atr"])
    atr_pct = (atr / current_price) * 100
    # SL = 2.0x ATR, зажатый между 1.5% и 4%
    sl = atr_pct * 2.0
    return round(max(1.5, min(4.0, sl)), 2)

# ============================================================
# ENTRY LOGIC
# ============================================================

def check_entry_conditions(features: dict, forecast: dict, params: dict) -> tuple[bool, str, str]:
    """
    Проверяет условия для входа.
    Возвращает (should_enter, direction, reason).
    """
    if not features or not forecast:
        return False, "", "no_data"

    # Базовые фильтры качества прогноза
    prob = forecast.get("direction_probability", 50)
    conf = forecast.get("confidence", 50)
    risk = forecast.get("risk_score", 100)
    direction_raw = forecast.get("direction", "neutral")

    min_prob = params.get("min_probability", 55)
    min_conf = params.get("min_confidence", 50)
    max_risk = params.get("max_risk_score", 75)

    if direction_raw == "neutral":
        return False, "", "neutral_forecast"
    if prob < min_prob:
        return False, "", f"low_prob({prob}<{min_prob})"
    if conf < min_conf:
        return False, "", f"low_conf({conf}<{min_conf})"
    if risk > max_risk:
        return False, "", f"high_risk({risk}>{max_risk})"

    direction = "long" if direction_raw == "up" else "short"

    # Fear & Greed блоки
    fg = features.get("fear_greed_index", 50)

    # Абсолютный блок LONG при FG < 40 (данные показали 0% win rate)
    if direction == "long" and fg < 40:
        return False, "", f"long_blocked_fg({fg}<40)"

    # S/R фильтр
    sr_signal = features.get("sr_signal", "neutral")
    sr_strength = features.get("sr_strength", 0)

    if direction == "short" and sr_signal == "bounce_support" and sr_strength >= 30:
        return False, "", "sr_near_support"
    if direction == "long" and sr_signal == "bounce_resistance" and sr_strength >= 30:
        return False, "", "sr_near_resistance"

    # Проверяем P10/P90 позицию (range entry)
    price = features.get("price", 0)
    p10 = forecast.get("p10", 0)
    p90 = forecast.get("p90", 0)

    if p90 > p10 and price > 0:
        corridor = p90 - p10
        pos = (price - p10) / corridor

        if direction == "long" and pos > 0.3:
            return False, "", f"long_not_at_low(pos={pos:.2f})"
        if direction == "short" and pos < 0.7:
            return False, "", f"short_not_at_high(pos={pos:.2f})"

    reason = f"range_{direction}(p={prob},c={conf},fg={fg})"
    return True, direction, reason

# ============================================================
# OPEN TRADE
# ============================================================

async def open_trade(account: dict, symbol: str, direction: str,
                      price: float, params: dict, forecast: dict, reason: str) -> dict | None:
    """Открывает демо-сделку."""
    balance = float(account["current_balance"])
    pos_pct = float(params.get("position_size_percent", 5)) / 100
    size = balance * pos_pct
    size = min(size, balance * 0.15)  # максимум 15% за раз

    if size < 10:
        return None

    amount_crypto = size / price
    sl_pct = await calc_atr_sl(symbol, price)

    trade_data = {
        "account_id": account["id"],
        "symbol": symbol,
        "trade_type": direction,
        "amount_usdt": round(size, 2),
        "amount_crypto": amount_crypto,
        "entry_price": price,
        "status": "open",
        "leverage": 1.0,
        "peak_pnl_usdt": 0.0,
        "trough_pnl_usdt": 0.0,
        "forecast_id": forecast.get("id"),
        "forecast_direction": forecast.get("direction"),
        "forecast_probability": forecast.get("direction_probability"),
        "mirrored_to_bybit": False,
    }

    row = await db.fetchrow(
        """INSERT INTO crypto_demo_trades
           (account_id, symbol, trade_type, amount_usdt, amount_crypto, entry_price,
            status, leverage, peak_pnl_usdt, trough_pnl_usdt,
            forecast_direction, forecast_probability, mirrored_to_bybit)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
           RETURNING *""",
        trade_data["account_id"], trade_data["symbol"], trade_data["trade_type"],
        trade_data["amount_usdt"], trade_data["amount_crypto"], trade_data["entry_price"],
        "open", 1.0, 0.0, 0.0,
        trade_data["forecast_direction"], trade_data["forecast_probability"], False
    )

    trade = dict(row) if row else None
    if trade:
        logger.info(f"OPENED {direction.upper()} {symbol} @ ${price:,.2f} size=${size:,.0f} sl={sl_pct}%")
        tp1 = float(params.get("tp1_percent", 2.0))
        await tg.send_message(
            tg.format_trade_open(symbol, direction, price, size, sl_pct, tp1),
            account.get("telegram_chat_id")
        )

    return trade

# ============================================================
# EXIT LOGIC — trailing stop как главный выход
# ============================================================

async def check_exit(trade: dict, current_price: float, params: dict,
                      account: dict) -> tuple[bool, str, float]:
    """
    Проверяет условия выхода.
    Возвращает (should_close, reason, close_percent).
    """
    entry = float(trade["entry_price"])
    size = float(trade["amount_usdt"])
    direction = trade["trade_type"]
    peak = float(trade.get("peak_pnl_usdt") or 0)

    # Текущий PnL
    crypto = float(trade["amount_crypto"])
    if direction == "long":
        pnl = (current_price - entry) * crypto
    else:
        pnl = (entry - current_price) * crypto

    pnl_pct = (pnl / size) * 100

    # Обновляем peak
    if pnl > peak:
        await db.execute(
            "UPDATE crypto_demo_trades SET peak_pnl_usdt=$1 WHERE id=$2",
            pnl, trade["id"]
        )
        peak = pnl

    peak_pct = (peak / size) * 100
    prev_reasons = trade.get("close_reason") or ""
    has_tp1 = "tp1" in prev_reasons
    has_tp2 = "tp2" in prev_reasons

    sl_pct = await calc_atr_sl(trade["symbol"], current_price)
    fee_pct = float(params.get("fee_rate_taker", 0.055)) * 2 + 0.1  # round-trip fees

    # 1. СТОП-ЛОСС
    if pnl_pct <= -sl_pct:
        return True, "stop_loss", 100

    # 2. BE-STOP после TP1
    if has_tp1 and params.get("be_stop_after_tp1", True):
        if pnl_pct <= fee_pct:
            return True, "breakeven_stop", 100

    # 3. TP1 (частичное закрытие)
    tp1_pct = float(params.get("tp1_percent", 2.0))
    if not has_tp1 and pnl_pct >= tp1_pct:
        close_pct = float(params.get("tp1_close_pct", 40))
        return True, "tp1_partial", close_pct

    # 4. TP2 (частичное закрытие)
    tp2_pct = float(params.get("tp2_percent", 4.0))
    if has_tp1 and not has_tp2 and pnl_pct >= tp2_pct:
        close_pct = float(params.get("tp2_close_pct", 30))
        return True, "tp2_partial", close_pct

    # 5. TRAILING STOP (главный выход — WR=100% в данных)
    trail_start = float(params.get("trail_start_percent", 1.0))
    if peak_pct >= trail_start:
        # ATR-based trail offset
        atr_row = await db.fetchrow(
            "SELECT atr, price FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
            trade["symbol"]
        )
        if atr_row and atr_row["atr"] and atr_row["price"]:
            atr_pct = float(atr_row["atr"]) / float(atr_row["price"]) * 100
            trail_offset = max(0.5, min(3.0, atr_pct * float(params.get("runner_trail_atr_mult", 1.2))))
        else:
            trail_offset = 0.8

        if pnl_pct <= peak_pct - trail_offset:
            return True, "trailing_stop", 100

    # 6. TRAILING BREAKEVEN (скользящий безубыток)
    # Если пик >= 3% но цена вернулась к низкому уровню — фиксируем минимум
    if not has_tp1 and peak_pct >= 3.0:
        sliding_floor = max(fee_pct, peak_pct * 0.5)
        if pnl_pct <= sliding_floor:
            return True, "trailing_breakeven", 100

    # 7. PEAK PROTECTION (защита прибыли)
    if peak_pct >= 5.0 and not has_tp1:
        if pnl_pct <= peak_pct - 2.5:
            return True, "peak_protection", 100

    return False, "", 0

# ============================================================
# CLOSE TRADE
# ============================================================

async def close_trade(trade: dict, exit_price: float, reason: str,
                       close_pct: float, account: dict, params: dict) -> float | None:
    """Закрывает или частично закрывает сделку."""
    entry = float(trade["entry_price"])
    size = float(trade["amount_usdt"])
    crypto = float(trade["amount_crypto"])
    direction = trade["trade_type"]
    fee_rate = float(params.get("fee_rate_taker", 0.055))

    if close_pct < 100:
        # Частичное закрытие
        close_frac = close_pct / 100
        closed_crypto = crypto * close_frac
        closed_usdt = size * close_frac

        if direction == "long":
            gross_pnl = (exit_price - entry) * closed_crypto
        else:
            gross_pnl = (entry - exit_price) * closed_crypto

        fees = closed_usdt * (2 * fee_rate / 100) + closed_usdt * 0.001
        pnl = gross_pnl - fees

        # Обновляем размер оставшейся позиции
        prev_reasons = trade.get("close_reason") or ""
        new_reason = f"{prev_reasons},{reason}" if prev_reasons else reason

        await db.execute(
            """UPDATE crypto_demo_trades
               SET amount_usdt=$1, amount_crypto=$2, close_reason=$3
               WHERE id=$4""",
            size - closed_usdt,
            crypto - closed_crypto,
            new_reason,
            trade["id"]
        )

        # Начисляем PnL на баланс
        await db.execute(
            "UPDATE crypto_demo_accounts SET current_balance=current_balance+$1 WHERE id=$2",
            pnl, account["id"]
        )

        remaining = size - closed_usdt
        await tg.send_message(
            tg.format_partial_close(trade["symbol"], int(close_pct), pnl, remaining, reason),
            account.get("telegram_chat_id")
        )

        logger.info(f"PARTIAL CLOSE {trade['symbol']} {close_pct}% pnl=${pnl:,.2f} [{reason}]")
        return pnl

    else:
        # Полное закрытие
        if direction == "long":
            gross_pnl = (exit_price - entry) * crypto
        else:
            gross_pnl = (entry - exit_price) * crypto

        fees = size * (2 * fee_rate / 100) + size * 0.001
        pnl = gross_pnl - fees
        pnl_pct = (pnl / size) * 100

        hold_h = (datetime.now(timezone.utc) - trade["opened_at"].replace(tzinfo=timezone.utc)).total_seconds() / 3600

        prev_reasons = trade.get("close_reason") or ""
        full_reason = f"{prev_reasons},{reason}" if prev_reasons else reason

        await db.execute(
            """UPDATE crypto_demo_trades
               SET exit_price=$1, pnl_usdt=$2, status='closed',
                   closed_at=now(), close_reason=$3
               WHERE id=$4""",
            exit_price, pnl, full_reason, trade["id"]
        )

        # Баланс пересчитается через триггер, но обновляем вручную тоже для надёжности
        await db.execute(
            "UPDATE crypto_demo_accounts SET current_balance=current_balance+$1 WHERE id=$2",
            pnl, account["id"]
        )

        new_balance = float(account["current_balance"]) + pnl
        await tg.send_message(
            tg.format_trade_close(
                trade["symbol"], direction, entry, exit_price,
                pnl, pnl_pct, reason, hold_h, new_balance
            ),
            account.get("telegram_chat_id")
        )

        emoji = "✅" if pnl > 0 else "❌"
        logger.info(f"{emoji} CLOSED {trade['symbol']} {direction.upper()} pnl=${pnl:,.2f} ({pnl_pct:.1f}%) [{reason}]")
        return pnl

# ============================================================
# ГЛАВНЫЙ ТОРГОВЫЙ ЦИКЛ
# ============================================================

_symbol_cooldowns: dict[str, datetime] = {}

def is_on_cooldown(symbol: str, cooldown_min: float) -> bool:
    if symbol not in _symbol_cooldowns:
        return False
    elapsed = (datetime.now(timezone.utc) - _symbol_cooldowns[symbol]).total_seconds() / 60
    return elapsed < cooldown_min

def mark_cooldown(symbol: str):
    _symbol_cooldowns[symbol] = datetime.now(timezone.utc)

async def run_trading_cycle():
    """Один торговый цикл."""
    params = await load_params()
    account = await get_account()
    if not account:
        logger.warning("No active account found")
        return

    banned = set(params.get("banned_symbols") or [])
    cooldown_min = float(params.get("symbol_cooldown_minutes", 30))
    forecast_max_age = float(params.get("forecast_max_age_minutes", 15))

    # ---- ВЫХОДЫ ----
    open_trades = await get_open_trades(account["id"])
    for trade in open_trades:
        price = await get_current_price(trade["symbol"])
        if not price:
            continue
        should_close, reason, close_pct = await check_exit(trade, price, params, account)
        if should_close:
            pnl = await close_trade(trade, price, reason, close_pct, account, params)
            if pnl is not None and pnl < 0:
                mark_cooldown(trade["symbol"])
            # Обновляем аккаунт после изменения баланса
            account = await get_account()

    # ---- ВХОДЫ ----
    open_trades = await get_open_trades(account["id"])
    open_symbols = {t["symbol"] for t in open_trades}

    if len(open_trades) >= MAX_OPEN_TRADES:
        logger.debug(f"Max open trades reached ({MAX_OPEN_TRADES})")
        return

    # Kill switch
    if params.get("kill_switch_active"):
        logger.info("Kill switch active, skipping entries")
        return

    # Суточная просадка
    daily_dd_limit = float(params.get("daily_drawdown_limit", 5))
    initial = float(account["initial_balance"])
    current = float(account["current_balance"])
    dd_pct = (initial - current) / initial * 100
    if dd_pct >= daily_dd_limit:
        logger.warning(f"Daily drawdown {dd_pct:.1f}% >= {daily_dd_limit}% — no new entries")
        return

    # Получаем активные символы
    symbols = await db.fetch(
        "SELECT symbol FROM crypto_assets WHERE is_active=true ORDER BY rank"
    )
    symbols = [r["symbol"] for r in symbols
               if r["symbol"] not in open_symbols
               and r["symbol"] not in banned
               and not is_on_cooldown(r["symbol"], cooldown_min)]

    new_trades = 0
    for symbol in symbols:
        if new_trades >= MAX_NEW_TRADES_PER_CYCLE:
            break

        # Фичи
        features_row = await db.fetchrow(
            "SELECT * FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
            symbol
        )
        if not features_row:
            continue
        features = dict(features_row)

        # Проверяем свежесть фичей (не старше 30 минут)
        age_min = (datetime.now(timezone.utc) - features["ts"].replace(tzinfo=timezone.utc)).total_seconds() / 60
        if age_min > 30:
            continue

        # Прогноз (4h горизонт)
        forecast = await get_latest_forecast(symbol, "4h")
        if not forecast:
            continue

        # Проверяем свежесть прогноза
        fc_age = (datetime.now(timezone.utc) - forecast["created_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60
        if fc_age > forecast_max_age:
            continue

        should_enter, direction, reason = check_entry_conditions(features, forecast, params)
        if not should_enter:
            continue

        # Кластерный лимит (не более 3 позиций в одном секторе)
        sector = SECTOR_MAP.get(symbol, "other")
        sector_count = sum(1 for t in open_trades if SECTOR_MAP.get(t["symbol"], "other") == sector)
        if sector_count >= params.get("max_positions_high_corr", 3):
            continue

        # Лимит экспозиции
        total_exposure = sum(float(t["amount_usdt"]) for t in open_trades)
        balance = float(account["current_balance"])
        pos_pct = float(params.get("position_size_percent", 5)) / 100
        new_size = balance * pos_pct
        max_exposure = balance * float(params.get("max_total_exposure", 25)) / 100

        if total_exposure + new_size > max_exposure:
            logger.debug(f"Exposure limit reached: ${total_exposure:,.0f}+${new_size:,.0f} > ${max_exposure:,.0f}")
            break

        price = await get_current_price(symbol)
        if not price:
            continue

        trade = await open_trade(account, symbol, direction, price, params, forecast, reason)
        if trade:
            new_trades += 1
            open_trades.append(trade)
            open_symbols.add(symbol)
            # Обновляем аккаунт
            account = await get_account()

    if new_trades > 0 or len(open_trades) > 0:
        logger.info(f"Cycle done: {new_trades} opened, {len(open_trades)} open, balance=${float(account['current_balance']):,.0f}")

async def run_trader():
    """Основной цикл трейдера — каждые 2 минуты."""
    logger.info("Trader started")

    # Создаём демо-аккаунт если его нет
    account = await get_account()
    if not account:
        chat_id = settings.telegram_chat_id or ""
        await db.execute(
            """INSERT INTO crypto_demo_accounts
               (initial_balance, current_balance, telegram_chat_id, is_active)
               VALUES ($1, $1, $2, true)
               ON CONFLICT DO NOTHING""",
            settings.demo_initial_balance, chat_id
        )
        logger.info(f"Created demo account with ${settings.demo_initial_balance:,.0f}")
        account = await get_account()

    if account and settings.telegram_chat_id:
        await tg.send_message(
            f"🤖 <b>Крипто-бот запущен</b>\n\n"
            f"💰 Баланс: ${float(account['current_balance']):,.0f}\n"
            f"📋 Режим: Demo\n"
            f"✅ Всё работает!",
            settings.telegram_chat_id
        )

    while True:
        try:
            await run_trading_cycle()
        except Exception as e:
            logger.error(f"Trading cycle error: {e}")
        await asyncio.sleep(120)  # каждые 2 минуты

import aiohttp
import asyncio
import logging
from datetime import datetime, timezone
from config import TRADING_INTERVAL, DEMO_INITIAL_BALANCE, TELEGRAM_CHAT_ID, BYBIT_SYMBOL_MAP
import db
import telegram_bot as tg
from forecaster import get_latest_forecast
from sr_engine import analyze_sr, get_sr_entry_signal, update_features_sr

logger = logging.getLogger("trader")

MAX_OPEN = 8
MAX_NEW_PER_CYCLE = 2

SECTOR = {
    "BTC": "btc", "ETH": "eth", "BNB": "exchange",
    "SOL": "alt-l1", "ADA": "alt-l1", "AVAX": "alt-l1", "DOT": "alt-l1",
    "XRP": "payments", "TRX": "payments", "LTC": "payments", "XLM": "payments",
    "DOGE": "meme", "SHIB": "meme", "PEPE": "meme", "BONK": "meme", "WIF": "meme",
    "LINK": "oracle", "UNI": "defi", "AAVE": "defi", "ARB": "l2", "OP": "l2",
    "NEAR": "alt-l1", "APT": "alt-l1", "SUI": "alt-l1", "ATOM": "alt-l1",
}

async def can_reenter(symbol: str, direction: str, forecast: dict) -> tuple[bool, str]:
    """
    Умная проверка переоткрытия — без cooldown по времени.
    Открываем снова только если сигнал реально подтверждает вход.
    """
    # 1. Прогноз должен быть свежим
    fc_age = (datetime.now(timezone.utc) - forecast["created_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60
    if fc_age > 15:
        return False, f"stale_forecast({fc_age:.0f}min)"

    # 2. Прогноз должен быть уверенным (не borderline)
    prob = float(forecast.get("direction_probability") or 50)
    if prob < 54:
        return False, f"weak_signal({prob:.0f}%)"

    # 3. Смотрим последнюю закрытую сделку по этому символу
    last_closed = await db.fetchrow(
        """SELECT exit_price, pnl_usdt, close_reason, closed_at
           FROM crypto_demo_trades
           WHERE symbol=$1 AND status='closed'
           ORDER BY closed_at DESC LIMIT 1""",
        symbol
    )

    if last_closed:
        # Если последняя сделка закрылась по стоп-лоссу — ждём разворота прогноза
        if last_closed["close_reason"] and "stop_loss" in last_closed["close_reason"]:
            # Требуем более сильный сигнал после стопа
            if prob < 56:
                return False, f"post_sl_weak({prob:.0f}%<56%)"

        # Если закрылись меньше 5 минут назад — нужен реально сильный сигнал
        age_min = (datetime.now(timezone.utc) - last_closed["closed_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60
        if age_min < 5 and prob < 57:
            return False, f"too_soon({age_min:.0f}min,{prob:.0f}%<57%)"

    return True, "ok"

def set_cooldown(symbol: str):
    pass  # Больше не нужен — логика в can_reenter

async def load_params() -> dict:
    row = await db.fetchrow("SELECT * FROM crypto_strategy_params WHERE id='current'")
    return dict(row) if row else {}

async def get_account() -> dict | None:
    row = await db.fetchrow("SELECT * FROM crypto_demo_accounts WHERE is_active=true LIMIT 1")
    return dict(row) if row else None

async def get_open_trades(account_id: str) -> list:
    rows = await db.fetch(
        "SELECT * FROM crypto_demo_trades WHERE account_id=$1 AND status='open'", account_id
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
    # Проверка минимальной цены для крупных монет
    min_prices = {"BTC": 1000, "ETH": 100, "BNB": 10, "SOL": 1}
    min_p = min_prices.get(symbol, 0.000001)
    if price < min_p:
        logger.warning(f"Suspicious price {symbol}: ${price} < min ${min_p}")
        return None
    return price

async def get_atr_sl(symbol: str, price: float) -> float:
    if price <= 0:
        return 2.5
    row = await db.fetchrow(
        "SELECT atr FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1", symbol
    )
    if not row or not row["atr"]:
        return 2.5
    atr_pct = float(row["atr"]) / price * 100
    return round(max(1.5, min(4.0, atr_pct * 2.0)), 2)

def get_allowed_direction(fg: float) -> str:
    """
    FG >= 50 → только LONG  (рынок жадный = растёт)
    FG 30-49 → оба          (нейтральная зона)
    FG < 30  → только SHORT (рынок в страхе = падает)
    """
    if fg >= 50:
        return "long_only"
    elif fg >= 30:
        return "both"
    else:
        return "short_only"

def get_min_probability(fg: float) -> float:
    return 65.0 if 30 <= fg < 50 else 55.0

def check_entry(features: dict, forecast: dict, params: dict) -> tuple:
    if not features or not forecast:
        return False, "", "no_data"

    prob = float(forecast.get("direction_probability") or 50)
    conf = float(forecast.get("confidence") or 50)
    risk = float(forecast.get("risk_score") or 100)
    direction_raw = forecast.get("direction", "neutral")

    if direction_raw == "neutral":
        return False, "", "neutral_forecast"

    fg = float(features.get("fear_greed_index") or 50)
    allowed = get_allowed_direction(fg)
    min_prob = get_min_probability(fg)
    direction = "long" if direction_raw == "up" else "short"

    if allowed == "long_only" and direction == "short":
        return False, "", f"short_blocked(FG={fg:.0f}>=50)"
    if allowed == "short_only" and direction == "long":
        return False, "", f"long_blocked(FG={fg:.0f}<30)"

    max_risk = float(params.get("max_risk_score") or 75)
    if prob < min_prob:
        return False, "", f"low_prob({prob:.0f}<{min_prob:.0f})"
    if conf < float(params.get("min_confidence") or 50):
        return False, "", f"low_conf({conf:.0f})"
    if risk > max_risk:
        return False, "", f"high_risk({risk:.0f})"

    sr_sig = features.get("sr_signal") or "neutral"
    sr_str = float(features.get("sr_strength") or 0)
    if direction == "short" and sr_sig == "bounce_support" and sr_str >= 30:
        return False, "", "sr_support_blocks_short"
    if direction == "long" and sr_sig == "bounce_resistance" and sr_str >= 30:
        return False, "", "sr_resistance_blocks_long"
    # Мягкий S/R фильтр - только блокируем противоположные сигналы
    # bounce_support при SHORT и bounce_resistance при LONG уже заблокированы выше

    # P10/P90 фильтр только в нейтральной зоне (both)
    # В trend режимах (long_only/short_only) цена может быть где угодно
    if allowed == "both":
        price = float(features.get("price") or 0)
        p10 = float(forecast.get("p10") or 0)
        p90 = float(forecast.get("p90") or 0)
        if p90 > p10 and price > 0:
            pos = (price - p10) / (p90 - p10)
            if direction == "long" and pos > 0.35:
                return False, "", f"long_not_low({pos:.2f})"
            if direction == "short" and pos < 0.65:
                return False, "", f"short_not_high({pos:.2f})"

    return True, direction, f"{direction}(p={prob:.0f},c={conf:.0f},fg={fg:.0f})"

async def open_trade(account, symbol, direction, price, params, forecast, sr_data=None):
    if not price or price <= 0:
        logger.warning(f"Invalid price {symbol}: {price}")
        return None

    balance = float(account["current_balance"])
    size = balance * float(params.get("position_size_percent") or 5) / 100
    size = min(size, balance * 0.10)
    if size < 10:
        return None

    crypto = size / price
    sl_pct = await get_atr_sl(symbol, price)
    tp1 = float(params.get("tp1_percent") or 2.0)

    row = await db.fetchrow(
        """INSERT INTO crypto_demo_trades
           (account_id,symbol,trade_type,amount_usdt,amount_crypto,entry_price,
            status,leverage,peak_pnl_usdt,trough_pnl_usdt,
            forecast_direction,forecast_probability,mirrored_to_bybit)
           VALUES ($1,$2,$3,$4,$5,$6,'open',1.0,0.0,0.0,$7,$8,false)
           RETURNING *""",
        account["id"], symbol, direction,
        round(size, 2), crypto, price,
        forecast.get("direction"), forecast.get("direction_probability")
    )
    if not row:
        return None

    # Используем S/R стоп если есть (точнее ATR стопа)
    sr_nearest = sr_data.get("nearest_support") if sr_data and direction=="long" else (sr_data.get("nearest_resistance") if sr_data else None)
    if sr_nearest:
        sr_level = float(sr_nearest["price"])
        sr_conf = float(sr_nearest.get("confluence_score", 0))
        logger.info(f"OPEN {direction.upper()} {symbol} @ ${price:,.4f} size=${size:,.0f} sl={sl_pct}% | SR level=${sr_level:,.4f} conf={sr_conf:.2f}")
    else:
        logger.info(f"OPEN {direction.upper()} {symbol} @ ${price:,.4f} size=${size:,.0f} sl={sl_pct}%")
    await tg.send(
        tg.fmt_open(symbol, direction, price, size, sl_pct, tp1),
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

    pnl = (price-entry)*crypto if direction=="long" else (entry-price)*crypto
    pnl_pct = pnl / size * 100

    if pnl > peak:
        await db.execute("UPDATE crypto_demo_trades SET peak_pnl_usdt=$1 WHERE id=$2", pnl, trade["id"])
        peak = pnl
    peak_pct = peak / size * 100

    prev = trade.get("close_reason") or ""
    has_tp1 = "tp1" in prev
    has_tp2 = "tp2" in prev
    fee_pct = float(params.get("fee_rate_taker") or 0.055) * 2 + 0.1
    sl_pct = await get_atr_sl(trade["symbol"], price)

    if pnl_pct <= -sl_pct:
        return True, "stop_loss", 100
    if has_tp1 and params.get("be_stop_after_tp1", True) and pnl_pct <= fee_pct:
        return True, "breakeven_stop", 100

    tp1 = float(params.get("tp1_percent") or 2.0)
    if not has_tp1 and pnl_pct >= tp1:
        return True, "tp1_partial", float(params.get("tp1_close_pct") or 40)

    tp2 = float(params.get("tp2_percent") or 4.0)
    if has_tp1 and not has_tp2 and pnl_pct >= tp2:
        return True, "tp2_partial", float(params.get("tp2_close_pct") or 30)

    trail_start = float(params.get("trail_start_percent") or 1.0)
    if peak_pct >= trail_start:
        atr_row = await db.fetchrow(
            "SELECT atr, price FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
            trade["symbol"]
        )
        offset = 0.8
        if atr_row and atr_row["atr"] and atr_row["price"]:
            atr_pct = float(atr_row["atr"]) / float(atr_row["price"]) * 100
            offset = max(0.5, min(3.0, atr_pct * float(params.get("runner_trail_atr_mult") or 1.2)))
        if pnl_pct <= peak_pct - offset:
            return True, "trailing_stop", 100

    if not has_tp1 and peak_pct >= 3.0:
        floor = max(fee_pct, peak_pct * 0.5)
        if pnl_pct <= floor:
            return True, "trailing_breakeven", 100

    if peak_pct >= 5.0 and not has_tp1 and pnl_pct <= peak_pct - 2.5:
        return True, "peak_protection", 100

    return False, "", 0

async def close_trade(trade, price, reason, close_pct, account, params):
    entry = float(trade["entry_price"])
    size = float(trade["amount_usdt"])
    crypto = float(trade["amount_crypto"])
    direction = trade["trade_type"]
    fee = float(params.get("fee_rate_taker") or 0.055)
    prev = trade.get("close_reason") or ""

    if close_pct < 100:
        frac = close_pct / 100
        closed_crypto = crypto * frac
        closed_usdt = size * frac
        gross = (price-entry)*closed_crypto if direction=="long" else (entry-price)*closed_crypto
        fees = closed_usdt * (2*fee/100) + closed_usdt * 0.001
        pnl = gross - fees
        new_reason = f"{prev},{reason}" if prev else reason
        await db.execute(
            "UPDATE crypto_demo_trades SET amount_usdt=$1,amount_crypto=$2,close_reason=$3 WHERE id=$4",
            size-closed_usdt, crypto-closed_crypto, new_reason, trade["id"]
        )
        await db.execute(
            "UPDATE crypto_demo_accounts SET current_balance=current_balance+$1 WHERE id=$2",
            pnl, account["id"]
        )
        await tg.send(tg.fmt_partial(trade["symbol"],int(close_pct),pnl,size-closed_usdt,reason),
                      account.get("telegram_chat_id") or TELEGRAM_CHAT_ID)
        logger.info(f"PARTIAL {trade['symbol']} {close_pct}% pnl=${pnl:,.2f} [{reason}]")
        return pnl
    else:
        gross = (price-entry)*crypto if direction=="long" else (entry-price)*crypto
        fees = size * (2*fee/100) + size * 0.001
        pnl = gross - fees
        pnl_pct = pnl / size * 100
        hold_h = (datetime.now(timezone.utc) - trade["opened_at"].replace(tzinfo=timezone.utc)).total_seconds() / 3600
        full_reason = f"{prev},{reason}" if prev else reason
        await db.execute(
            "UPDATE crypto_demo_trades SET exit_price=$1,pnl_usdt=$2,status='closed',closed_at=now(),close_reason=$3 WHERE id=$4",
            price, pnl, full_reason, trade["id"]
        )
        await db.execute(
            "UPDATE crypto_demo_accounts SET current_balance=current_balance+$1 WHERE id=$2",
            pnl, account["id"]
        )
        new_bal = float(account["current_balance"]) + pnl
        await tg.send(
            tg.fmt_close(trade["symbol"],direction,entry,price,pnl,pnl_pct,reason,hold_h,new_bal),
            account.get("telegram_chat_id") or TELEGRAM_CHAT_ID
        )
        e = "✅" if pnl > 0 else "❌"
        logger.info(f"{e} CLOSE {trade['symbol']} {direction.upper()} pnl=${pnl:,.2f} ({pnl_pct:.1f}%) [{reason}]")
        return pnl

async def fast_exit_check(prices: dict):
    """Быстрая проверка по WebSocket ценам каждые 10 секунд."""
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
    params = await load_params()
    account = await get_account()
    if not account:
        return

    banned = set(params.get("banned_symbols") or [])
    cooldown_min = float(params.get("symbol_cooldown_minutes") or 30)
    fc_max_age = float(params.get("forecast_max_age_minutes") or 15)

    open_trades = await get_open_trades(account["id"])
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
        return

    open_trades = await get_open_trades(account["id"])
    open_syms = {t["symbol"] for t in open_trades}

    if len(open_trades) >= MAX_OPEN:
        return

    balance = float(account["current_balance"])
    initial = float(account["initial_balance"])
    dd_pct = (initial - balance) / initial * 100 if initial > 0 else 0
    if dd_pct >= float(params.get("daily_drawdown_limit") or 5):
        logger.warning(f"Daily drawdown {dd_pct:.1f}% — no new entries")
        return

    fg_row = await db.fetchrow("SELECT value FROM crypto_fear_greed WHERE id='latest'")
    fg = float(fg_row["value"]) if fg_row else 50.0
    allowed = get_allowed_direction(fg)
    logger.info(f"Cycle: FG={fg:.0f} → {allowed} | open={len(open_trades)}/{MAX_OPEN}")

    symbols = await db.fetch("SELECT symbol FROM crypto_assets WHERE is_active=true ORDER BY rank")
    candidates = [r["symbol"] for r in symbols
                  if r["symbol"] not in open_syms
                  and r["symbol"] not in banned]

    new_trades = 0
    for symbol in candidates:
        if new_trades >= MAX_NEW_PER_CYCLE:
            break

        f_row = await db.fetchrow(
            "SELECT * FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1", symbol
        )
        if not f_row:
            continue
        features = dict(f_row)

        age = (datetime.now(timezone.utc) - features["ts"].replace(tzinfo=timezone.utc)).total_seconds() / 60
        if age > 30:
            continue

        forecast = await get_latest_forecast(symbol, "4h")
        if not forecast:
            continue
        fc_age = (datetime.now(timezone.utc) - forecast["created_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60
        if fc_age > fc_max_age:
            continue

        should, direction, reason = check_entry(features, forecast, params)
        if not should:
            continue

        # Умная проверка переоткрытия (без cooldown по времени)
        can_open, reentry_reason = await can_reenter(symbol, direction, forecast)
        if not can_open:
            logger.info(f"BLOCKED {symbol}: {reentry_reason}")
            continue

        # Нет противоположных позиций в одном секторе
        sector = SECTOR.get(symbol, "other")
        sector_trades = [t for t in open_trades if SECTOR.get(t["symbol"], "other") == sector]
        if any(t["trade_type"] != direction for t in sector_trades):
            continue
        if len(sector_trades) >= int(params.get("max_positions_high_corr") or 3):
            continue

        exposure = sum(float(t["amount_usdt"]) for t in open_trades)
        size = balance * float(params.get("position_size_percent") or 5) / 100
        max_exp = balance * float(params.get("max_total_exposure") or 25) / 100
        if exposure + size > max_exp:
            break

        price = await get_price(symbol)
        if not price:
            continue

        # S/R анализ — проверяем качество входа
        sr_data = None
        try:
            async with aiohttp.ClientSession() as sr_session:
                sr_data = await analyze_sr(sr_session, symbol)
                if sr_data:
                    await update_features_sr(symbol, sr_data)
        except Exception as e:
            logger.debug(f"SR analysis error {symbol}: {e}")

        # Проверяем S/R сигнал
        sr_ok, sl_price, sr_reason = get_sr_entry_signal(sr_data, direction)
        if sr_data and not sr_ok:
            logger.info(f"SR blocked {symbol} {direction}: {sr_reason}")
            continue

        trade = await open_trade(account, symbol, direction, price, params, forecast, sr_data)
        if trade:
            new_trades += 1
            open_trades.append(trade)
            open_syms.add(symbol)
            account = await get_account()

async def run_trader():
    logger.info("Trader started")
    account = await get_account()
    if not account:
        count = await db.fetchval("SELECT COUNT(*) FROM crypto_demo_accounts")
        if count == 0:
            await db.execute(
                "INSERT INTO crypto_demo_accounts (initial_balance,current_balance,telegram_chat_id,is_active) VALUES ($1,$1,$2,true)",
                DEMO_INITIAL_BALANCE, TELEGRAM_CHAT_ID or ""
            )
            logger.info(f"Demo account created: ${DEMO_INITIAL_BALANCE:,.0f}")
        else:
            await db.execute(
                "UPDATE crypto_demo_accounts SET is_active=true WHERE id=(SELECT id FROM crypto_demo_accounts ORDER BY created_at LIMIT 1)"
            )
            logger.info("Reactivated existing account")
        account = await get_account()

    if account:
        await tg.send(
            f"🤖 <b>Криптобот v1.1 запущен</b>\n\n"
            f"💰 Баланс: ${float(account['current_balance']):,.0f}\n"
            f"📋 Стратегия: FG-based direction\n"
            f"  FG≥50 → LONG only\n"
            f"  FG 30-49 → LONG+SHORT (prob≥65%)\n"
            f"  FG<30 → SHORT only",
            account.get("telegram_chat_id") or TELEGRAM_CHAT_ID
        )

    await asyncio.sleep(120)

    while True:
        try:
            await trading_cycle()
        except Exception as e:
            logger.error(f"Trading cycle error: {e}")
        await asyncio.sleep(TRADING_INTERVAL)

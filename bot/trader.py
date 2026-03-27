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

logger = logging.getLogger("trader")

MAX_OPEN = 100
MAX_NEW_PER_CYCLE = 2

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


def check_entry(features: dict, forecast: dict, params: dict) -> tuple[bool, str, str]:
    """
    Усиленная логика входа v2
    """

    if not forecast:
        return False, "", "no_data"

    try:
        prob = float(forecast.get("direction_probability") or 0)
    except:
        prob = 0.0

    fc_direction = str(forecast.get("direction") or "").lower().strip()

    # нормализуем направление
    if fc_direction in ("long", "up", "bull", "bullish", "buy"):
        direction = "long"
    elif fc_direction in ("short", "down", "bear", "bearish", "sell"):
        direction = "short"
    else:
        return False, "", "neutral_forecast"

    # =========================
    # 1. PROBABILITY
    # =========================
    min_prob = float(params.get("min_forecast_probability") or 75)

    if prob < min_prob:
        return False, "", f"weak_prob({prob:.1f}<{min_prob})"

    # =========================
    # 2. REGIME FILTER
    # =========================
    regime = str(features.get("regime") or "")

    if regime == "crash" and direction == "long":
        return False, "", "blocked_long_in_crash"

    # =========================
    # 3. MOMENTUM
    # =========================
    r_1h = float(features.get("r_1h") or 0)

    if direction == "long" and r_1h < 0:
        return False, "", f"bad_momentum_long({r_1h:.2f})"

    if direction == "short" and r_1h > 0:
        return False, "", f"bad_momentum_short({r_1h:.2f})"

    # =========================
    # 4. RSI FILTER
    # =========================
    rsi = float(features.get("rsi_14") or 50)
    # более строгий RSI-фильтр

    # не лонгуем перекупленное
    if direction == "long" and rsi > 70:
        return False, "", f"overbought_block(rsi={rsi:.1f})"

    # не шортим перепроданное
    if direction == "short" and rsi < 40:
        return False, "", f"oversold_block(rsi={rsi:.1f})"

    # =========================
    # 5. VOLUME FILTER
    # =========================
    volume = float(features.get("volume_24h") or 0)

    if volume < 1_000_000:
        return False, "", f"low_volume({volume:.0f})"

    # =========================
    # OK → ВХОД
    # =========================
    return True, direction, f"entry_ok(prob={prob:.1f})"


async def can_reenter(symbol: str, direction: str, forecast: dict) -> tuple[bool, str]:
    """
    Умная проверка переоткрытия — без cooldown по времени.
    Открываем снова только если сигнал реально подтверждает вход.
    """
    fc_age = (datetime.now(timezone.utc) - forecast["created_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60
    if fc_age > 15:
        return False, f"stale_forecast({fc_age:.0f}min)"

    prob = float(forecast.get("direction_probability") or 50)

    fg_row = await db.fetchrow("SELECT value FROM crypto_fear_greed WHERE id='latest'")
    fg = float(fg_row["value"]) if fg_row else 50.0

    min_prob = 75
    if prob < min_prob:
        return False, f"weak_signal({prob:.0f}%<{min_prob}%)"

    last_closed = await db.fetchrow(
        """SELECT exit_price, pnl_usdt, close_reason, closed_at
           FROM crypto_demo_trades
           WHERE symbol=$1 AND status='closed'
           ORDER BY closed_at DESC LIMIT 1""",
        symbol
    )

    if last_closed:
        if last_closed["close_reason"] and "stop_loss" in last_closed["close_reason"]:
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


async def get_sl_price(symbol: str, price: float, direction: str) -> tuple[float, float]:
    """
    Фиксированный стоп 15% от цены входа.
    """
    sl_pct = 15.0

    if direction == "short":
        return price * (1 + sl_pct / 100), sl_pct
    return price * (1 - sl_pct / 100), sl_pct


async def get_atr_sl(symbol: str, price: float) -> float:
    _, sl_pct = await get_sl_price(symbol, price, "short")
    return sl_pct


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
    sl_price, sl_pct = await get_sl_price(symbol, price, direction)
    tp1 = float(params.get("tp1_percent") or 2.0)
    
    row = await db.fetchrow(
        """INSERT INTO crypto_demo_trades
           (account_id,symbol,trade_type,amount_usdt,amount_crypto,entry_price,
            sl_price,
            status,leverage,peak_pnl_usdt,trough_pnl_usdt,
            forecast_id,forecast_direction,forecast_probability,features_snapshot,mirrored_to_bybit)
           VALUES ($1,$2,$3,$4,$5,$6,$7,'open',1.0,0.0,0.0,$8,$9,$10,$11,false)
           RETURNING *""",
        account["id"], symbol, direction,
        round(size, 2), crypto, price,
        sl_price,
        forecast.get("id"),
        forecast.get("direction"),
        forecast.get("direction_probability"),
        json.dumps(forecast.get("features_snapshot") or {})
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
            f"sl={sl_pct}% | SR level=${sr_level:,.4f} conf={sr_conf:.2f}"
        )
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

    pnl = (price - entry) * crypto if direction == "long" else (entry - price) * crypto
    pnl_pct = pnl / size * 100

    if pnl > peak:
        await db.execute("UPDATE crypto_demo_trades SET peak_pnl_usdt=$1 WHERE id=$2", pnl, trade["id"])
        peak = pnl

    peak_pct = peak / size * 100

    prev = trade.get("close_reason") or ""
    has_tp1 = "tp1" in prev
    has_tp2 = "tp2" in prev
    fee_pct = float(params.get("fee_rate_taker") or 0.055) * 2 + 0.1
    sl_price = float(trade.get("sl_price") or 0)

    if sl_price > 0:
        if direction == "long" and price <= sl_price:
            return True, "stop_loss", 100
        if direction == "short" and price >= sl_price:
            return True, "stop_loss", 100

    # Авто-выход по деградации / развороту прогноза
    latest_fc = await get_latest_forecast(trade["symbol"], "4h")
    if latest_fc:
        fc_dir = str(latest_fc.get("direction") or "").lower().strip()
        fc_prob = float(latest_fc.get("direction_probability") or 0)

        # 1) Жесткий выход при сильном противоположном прогнозе
        if direction == "long" and fc_dir == "down" and fc_prob >= 75:
            return True, f"opposite_forecast_exit({fc_prob:.1f})", 100

        if direction == "short" and fc_dir == "up" and fc_prob >= 75:
            return True, f"opposite_forecast_exit({fc_prob:.1f})", 100
            
        # 2) Ранний выход: сделка уже в минусе, а прогноз заметно ослаб
        if pnl_pct <= -1.5 and fc_prob < 70:
            return True, f"weak_forecast_exit({fc_prob:.1f})", 100

        # 3) Совсем слабый сигнал / neutral — выходим почти сразу
        if pnl_pct <= -0.5 and fc_prob <= 50:
            return True, f"forecast_decay_exit({fc_prob:.1f})", 100

    if has_tp1 and params.get("be_stop_after_tp1", True) and pnl_pct <= fee_pct:
        return True, "breakeven_stop", 100

    sr_features = await db.fetchrow(
        "SELECT support_1, resistance_1 FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
        trade["symbol"]
    )
    if sr_features:
        # LONG: закрываемся полностью, когда почти дошли до сопротивления
        if direction == "long" and sr_features["resistance_1"]:
            sr_tp = float(sr_features["resistance_1"])
            sr_tp_pct = (sr_tp - entry) / entry * 100

            # Имеет смысл только если сопротивление действительно выше входа
            if sr_tp_pct >= 0.5:
                # если до сопротивления осталось <= 0.35%, закрываемся полностью
                if price >= sr_tp * 0.9965:
                    return True, "tp_sr_resistance", 100

        # SHORT: закрываемся полностью, когда почти дошли до поддержки
        elif direction == "short" and sr_features["support_1"]:
            sr_tp = float(sr_features["support_1"])
            sr_tp_pct = (entry - sr_tp) / entry * 100

            # Имеет смысл только если поддержка действительно ниже входа
            if sr_tp_pct >= 0.5:
                # если до поддержки осталось <= 0.35%, закрываемся полностью
                if price <= sr_tp * 1.0035:
                    return True, "tp_sr_support", 100
    # Старые partial TP отключены.
    # Теперь основной выход:
    # 1) у цели по S/R
    # 2) по trailing после нормального движения
    # 3) по stop-loss

    trail_start = float(params.get("trail_start_percent") or 2.5)
    if peak_pct >= trail_start:
        atr_row = await db.fetchrow(
            "SELECT atr, price FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
            trade["symbol"]
        )

        # Даём сделке дышать заметно больше, чем раньше
        offset = 1.8

        if atr_row and atr_row["atr"] and atr_row["price"]:
            atr_pct = float(atr_row["atr"]) / float(atr_row["price"]) * 100
            offset = max(1.2, min(4.5, atr_pct * float(params.get("runner_trail_atr_mult") or 1.8)))

        if pnl_pct <= peak_pct - offset:
            return True, "trailing_stop", 100

    # Мягкая защита прибыли только после заметного движения.
    # Раньше срабатывало слишком рано и душило хорошие сделки.
    if peak_pct >= 5.0:
        floor = max(1.0, peak_pct * 0.35)
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

    # Реалистичное проскальзывание на выходе
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
    logger.info("Cycle start")
    params = await load_params()
    logger.info("Cycle params loaded")
    account = await get_account()
    logger.info("Cycle account loaded")
    if not account:
        logger.info("Cycle stopped: no account")
        return

    banned = set(params.get("banned_symbols") or [])
    fc_max_age = float(params.get("forecast_max_age_minutes") or 15)

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
    if dd_pct >= float(params.get("daily_drawdown_limit") or 5):
        logger.warning(f"Daily drawdown {dd_pct:.1f}% — no new entries")
        return

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

    new_trades = 0
    for symbol in candidates:
        if new_trades >= MAX_NEW_PER_CYCLE:
            break

        f_row = await db.fetchrow(
            "SELECT * FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
            symbol
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
            if reason not in ("neutral_forecast", "no_data"):
                logger.info(f"SKIP {symbol}: {reason}")
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

        exposure = sum(float(t["amount_usdt"]) for t in open_trades)
        size = balance * float(params.get("position_size_percent") or 5) / 100
        max_exp = balance * float(params.get("max_total_exposure") or 25) / 100
        if exposure + size > max_exp:
            logger.info("Cycle stopped: max exposure reached")
            break

        price = await get_price(symbol)
        if not price:
            continue

        sr_data = None
        try:
            async with aiohttp.ClientSession() as sr_session:
                sr_data = await analyze_sr(sr_session, symbol)
                if sr_data:
                    await update_features_sr(symbol, sr_data)
        except Exception as e:
            logger.debug(f"SR analysis error {symbol}: {e}")

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

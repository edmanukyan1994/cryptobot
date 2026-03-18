import asyncio
import logging
import aiohttp
from datetime import datetime, timezone
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
import db

logger = logging.getLogger("tg_commands")

# Последний update_id для polling
_last_update_id = 0

COMMANDS_HELP = """
🤖 <b>Команды бота:</b>

/status — общий статус и баланс
/positions — открытые позиции с PnL
/balance — история изменения баланса
/stats — статистика сделок
/closeall — закрыть все позиции
/help — это сообщение
"""

async def api_call(method: str, data: dict = None) -> dict:
    """Вызов Telegram API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=data or {}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return await resp.json()
    except Exception as e:
        logger.warning(f"Telegram API error: {e}")
        return {}

async def send(chat_id: str, text: str):
    await api_call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

async def cmd_status(chat_id: str):
    account = await db.fetchrow("SELECT * FROM crypto_demo_accounts WHERE is_active=true LIMIT 1")
    if not account:
        await send(chat_id, "❌ Аккаунт не найден")
        return

    balance = float(account["current_balance"])
    initial = float(account["initial_balance"])
    pnl = balance - initial
    pnl_pct = pnl / initial * 100
    sign = "+" if pnl >= 0 else ""

    open_count = await db.fetchval(
        "SELECT COUNT(*) FROM crypto_demo_trades WHERE status='open'"
    )
    closed_count = await db.fetchval(
        "SELECT COUNT(*) FROM crypto_demo_trades WHERE status='closed'"
    )

    fg_row = await db.fetchrow("SELECT value, label FROM crypto_fear_greed WHERE id='latest'")
    fg = float(fg_row["value"]) if fg_row else 50
    fg_label = fg_row["label"] if fg_row else "Unknown"

    params = await db.fetchrow("SELECT strategy_mode, kill_switch_active FROM crypto_strategy_params WHERE id='current'")
    mode = params["strategy_mode"] if params else "unknown"
    kill = "🛑 KILL SWITCH" if params and params["kill_switch_active"] else "✅ Активен"

    await send(chat_id,
        f"📊 <b>Статус бота</b>\n\n"
        f"💰 Баланс: ${balance:,.0f}\n"
        f"📈 PnL: {sign}${pnl:,.0f} ({sign}{pnl_pct:.2f}%)\n"
        f"🔄 Открытых: {open_count} | Закрытых: {closed_count}\n\n"
        f"😰 Fear&Greed: {fg:.0f} ({fg_label})\n"
        f"📋 Режим: {mode}\n"
        f"🔌 Статус: {kill}"
    )

async def cmd_positions(chat_id: str):
    trades = await db.fetch(
        "SELECT * FROM crypto_demo_trades WHERE status='open' ORDER BY opened_at"
    )
    if not trades:
        await send(chat_id, "📭 Нет открытых позиций")
        return

    lines = ["📋 <b>Открытые позиции:</b>\n"]
    total_pnl = 0

    for t in trades:
        price_row = await db.fetchrow(
            "SELECT price FROM crypto_prices_bybit WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
            t["symbol"]
        )
        if not price_row:
            continue

        current = float(price_row["price"])
        entry = float(t["entry_price"])
        size = float(t["amount_usdt"])
        crypto = float(t["amount_crypto"])

        if t["trade_type"] == "long":
            pnl = (current - entry) * crypto
        else:
            pnl = (entry - current) * crypto

        pnl_pct = pnl / size * 100
        total_pnl += pnl
        sign = "+" if pnl >= 0 else ""
        emoji = "🟢" if t["trade_type"] == "long" else "🔴"
        hold_h = (datetime.now(timezone.utc) - t["opened_at"].replace(tzinfo=timezone.utc)).total_seconds() / 3600

        lines.append(
            f"{emoji} <b>{t['symbol']}</b> {t['trade_type'].upper()}\n"
            f"   Вход: ${entry:,.4f} → ${current:,.4f}\n"
            f"   PnL: {sign}${pnl:,.0f} ({sign}{pnl_pct:.2f}%) | {hold_h:.1f}ч\n"
        )

    sign_total = "+" if total_pnl >= 0 else ""
    lines.append(f"\n💼 Итого: {sign_total}${total_pnl:,.0f}")
    await send(chat_id, "\n".join(lines))

async def cmd_balance(chat_id: str):
    # Последние 10 закрытых сделок
    trades = await db.fetch(
        """SELECT symbol, trade_type, pnl_usdt, close_reason, closed_at
           FROM crypto_demo_trades
           WHERE status='closed' AND pnl_usdt IS NOT NULL
           ORDER BY closed_at DESC LIMIT 10"""
    )

    account = await db.fetchrow("SELECT current_balance, initial_balance FROM crypto_demo_accounts WHERE is_active=true LIMIT 1")
    balance = float(account["current_balance"]) if account else 0
    initial = float(account["initial_balance"]) if account else 0

    if not trades:
        await send(chat_id, f"💰 Баланс: ${balance:,.0f}\nЗакрытых сделок пока нет")
        return

    lines = [f"💰 <b>Баланс: ${balance:,.0f}</b>\n"]
    lines.append("📜 Последние сделки:\n")

    for t in trades:
        pnl = float(t["pnl_usdt"])
        sign = "+" if pnl >= 0 else ""
        emoji = "✅" if pnl >= 0 else "❌"
        reason = t["close_reason"].split(",")[-1] if t["close_reason"] else "?"
        lines.append(f"{emoji} {t['symbol']} {sign}${pnl:,.0f} [{reason}]")

    pnl_total = balance - initial
    sign_total = "+" if pnl_total >= 0 else ""
    lines.append(f"\n📊 Итого PnL: {sign_total}${pnl_total:,.0f} ({sign_total}{pnl_total/initial*100:.2f}%)")
    await send(chat_id, "\n".join(lines))

async def cmd_stats(chat_id: str):
    total = await db.fetchval("SELECT COUNT(*) FROM crypto_demo_trades WHERE status='closed'")
    if not total:
        await send(chat_id, "📊 Статистика пока недоступна — нет закрытых сделок")
        return

    wins = await db.fetchval("SELECT COUNT(*) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt > 0")
    total_pnl = await db.fetchval("SELECT SUM(pnl_usdt) FROM crypto_demo_trades WHERE status='closed'")
    avg_win = await db.fetchval("SELECT AVG(pnl_usdt) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt > 0")
    avg_loss = await db.fetchval("SELECT AVG(pnl_usdt) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt < 0")

    wr = wins / total * 100 if total > 0 else 0
    total_pnl = float(total_pnl or 0)
    avg_win = float(avg_win or 0)
    avg_loss = float(avg_loss or 0)
    sign = "+" if total_pnl >= 0 else ""

    # Топ символы
    top = await db.fetch(
        """SELECT symbol, SUM(pnl_usdt) as total, COUNT(*) as cnt
           FROM crypto_demo_trades WHERE status='closed'
           GROUP BY symbol ORDER BY total DESC LIMIT 5"""
    )

    lines = [
        f"📊 <b>Статистика</b>\n",
        f"Сделок: {total} (✅{wins} / ❌{total-wins})",
        f"Win Rate: {wr:.1f}%",
        f"Общий PnL: {sign}${total_pnl:,.0f}",
        f"Средний выигрыш: +${avg_win:,.0f}",
        f"Средний проигрыш: -${abs(avg_loss):,.0f}",
    ]

    if avg_loss != 0:
        payoff = abs(avg_win / avg_loss)
        lines.append(f"Payoff ratio: {payoff:.2f}")

    if top:
        lines.append("\n🏆 Топ символы:")
        for r in top:
            s = "+" if float(r["total"]) >= 0 else ""
            lines.append(f"  {r['symbol']}: {s}${float(r['total']):,.0f} ({r['cnt']} сд.)")

    await send(chat_id, "\n".join(lines))

async def cmd_closeall(chat_id: str):
    """Активирует kill switch — бот перестаёт открывать новые сделки."""
    await db.execute(
        "UPDATE crypto_strategy_params SET kill_switch_active=true WHERE id='current'"
    )
    open_count = await db.fetchval(
        "SELECT COUNT(*) FROM crypto_demo_trades WHERE status='open'"
    )
    await send(chat_id,
        f"🛑 <b>Kill switch активирован</b>\n\n"
        f"Новые сделки не будут открываться.\n"
        f"Открытых позиций: {open_count} (закроются по стандартной логике)\n\n"
        f"Для возобновления торговли напиши /resume"
    )

async def cmd_resume(chat_id: str):
    await db.execute(
        "UPDATE crypto_strategy_params SET kill_switch_active=false WHERE id='current'"
    )
    await send(chat_id, "✅ Торговля возобновлена")

async def handle_command(chat_id: str, text: str):
    """Обрабатывает команду от пользователя."""
    cmd = text.strip().lower().split()[0] if text.strip() else ""

    # Проверяем что команда от авторизованного пользователя
    if str(chat_id) != str(TELEGRAM_CHAT_ID) and TELEGRAM_CHAT_ID:
        await send(chat_id, "❌ Нет доступа")
        return

    handlers = {
        "/status": cmd_status,
        "/positions": cmd_positions,
        "/balance": cmd_balance,
        "/stats": cmd_stats,
        "/closeall": cmd_closeall,
        "/resume": cmd_resume,
        "/help": lambda cid: send(cid, COMMANDS_HELP),
        "/start": lambda cid: send(cid, f"👋 Привет! Я криптобот.\n{COMMANDS_HELP}"),
    }

    handler = handlers.get(cmd)
    if handler:
        await handler(chat_id)
    else:
        await send(chat_id, f"❓ Неизвестная команда: {cmd}\n{COMMANDS_HELP}")

async def run_telegram_commands():
    """Polling loop для Telegram команд."""
    global _last_update_id

    if not TELEGRAM_BOT_TOKEN:
        logger.info("Telegram token not set, command bot disabled")
        return

    logger.info("Telegram command bot started")

    while True:
        try:
            result = await api_call("getUpdates", {
                "offset": _last_update_id + 1,
                "timeout": 30,
                "allowed_updates": ["message"]
            })

            if result.get("ok") and result.get("result"):
                for update in result["result"]:
                    _last_update_id = update["update_id"]
                    message = update.get("message", {})
                    text = message.get("text", "")
                    chat_id = str(message.get("chat", {}).get("id", ""))

                    if text and text.startswith("/"):
                        logger.info(f"Command from {chat_id}: {text}")
                        await handle_command(chat_id, text)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Telegram polling error: {e}")
            await asyncio.sleep(5)

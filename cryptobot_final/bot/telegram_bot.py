import aiohttp
from loguru import logger
from config import settings

TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

async def send_message(text: str, chat_id: str = None) -> bool:
    """Отправляет сообщение в Telegram."""
    cid = chat_id or settings.telegram_chat_id
    if not settings.telegram_bot_token or not cid:
        logger.debug(f"Telegram not configured, skipping: {text[:50]}")
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning(f"Telegram error: {data.get('description')}")
                    return False
                return True
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False

def format_trade_open(symbol: str, direction: str, price: float,
                       size: float, sl_pct: float, tp1_pct: float) -> str:
    emoji = "🟢" if direction == "long" else "🔴"
    dir_ru = "Покупка (рост)" if direction == "long" else "Продажа (падение)"
    return (
        f"{emoji} <b>Новая сделка: {symbol}</b>\n\n"
        f"📊 {dir_ru} по ${price:,.2f}\n"
        f"💰 Размер: ${size:,.0f}\n"
        f"🛑 Стоп: -{sl_pct:.1f}% | 🎯 TP1: +{tp1_pct:.1f}%\n"
        f"📋 Режим: Demo"
    )

def format_trade_close(symbol: str, direction: str, entry: float, exit_price: float,
                        pnl: float, pnl_pct: float, reason: str, hold_h: float,
                        balance: float) -> str:
    emoji = "✅" if pnl >= 0 else "❌"
    dir_ru = "Покупка" if direction == "long" else "Продажа"
    sign = "+" if pnl >= 0 else ""
    hold_str = f"{hold_h:.1f}ч" if hold_h >= 1 else f"{hold_h*60:.0f}мин"

    reason_map = {
        "stop_loss": "🛑 Стоп-лосс",
        "tp1_partial": "🎯 Первая цель",
        "tp2_partial": "🎯 Вторая цель",
        "trailing_stop": "📉 Трейлинг-стоп",
        "peak_protection": "🛡️ Защита прибыли",
        "breakeven_stop": "🔒 Безубыток",
        "trailing_breakeven": "🔐 Скользящий безубыток",
    }
    reason_text = reason_map.get(reason, reason)

    return (
        f"{emoji} <b>Сделка закрыта: {symbol}</b>\n\n"
        f"📊 {dir_ru}: ${entry:,.2f} → ${exit_price:,.2f}\n"
        f"{'📈' if pnl >= 0 else '📉'} Результат: {sign}${pnl:,.2f} ({sign}{pnl_pct:.2f}%)\n"
        f"⏱ Время: {hold_str}\n"
        f"📝 {reason_text}\n"
        f"💰 Баланс: ${balance:,.0f}"
    )

def format_partial_close(symbol: str, pct: int, pnl: float, remaining: float, reason: str) -> str:
    emoji = "✅" if pnl >= 0 else "⚠️"
    sign = "+" if pnl >= 0 else ""
    reason_map = {
        "tp1_partial": "🎯 Первая цель",
        "tp2_partial": "🎯 Вторая цель",
    }
    reason_text = reason_map.get(reason, reason)
    return (
        f"{emoji} <b>Частичное закрытие {symbol}</b> ({pct}%)\n"
        f"{'📈' if pnl >= 0 else '📉'} Результат: {sign}${pnl:,.2f}\n"
        f"💰 Осталось в сделке: ${remaining:,.0f}\n"
        f"📝 {reason_text}"
    )

def format_status(balance: float, initial: float, open_trades: int,
                   fg: float, regime: str) -> str:
    pnl = balance - initial
    pnl_pct = pnl / initial * 100
    sign = "+" if pnl >= 0 else ""
    fg_label = "😨 Страх" if fg < 25 else "😐 Нейтральный" if fg < 55 else "😄 Жадность"
    return (
        f"📊 <b>Статус бота</b>\n\n"
        f"💰 Баланс: ${balance:,.0f}\n"
        f"📈 PnL: {sign}${pnl:,.0f} ({sign}{pnl_pct:.1f}%)\n"
        f"🔄 Открытых позиций: {open_trades}\n"
        f"😰 Fear&Greed: {fg:.0f} ({fg_label})\n"
        f"📋 Режим рынка: {regime}"
    )

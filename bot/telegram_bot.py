import aiohttp
import logging
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("telegram")

async def send(text: str, chat_id: str = None) -> bool:
    cid = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not cid:
        logger.debug(f"Telegram not configured: {text[:50]}")
        return False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return data.get("ok", False)
    except Exception as e:
        logger.warning(f"Telegram error: {e}")
        return False

def fmt_open(symbol, direction, price, size, sl_pct, tp1_pct):
    e = "🟢" if direction == "long" else "🔴"
    d = "Покупка (рост)" if direction == "long" else "Продажа (падение)"
    return (f"{e} <b>Новая сделка: {symbol}</b>\n\n"
            f"📊 {d} по ${price:,.2f}\n"
            f"💰 Размер: ${size:,.0f}\n"
            f"🛑 Стоп: -{sl_pct:.1f}% | 🎯 TP1: +{tp1_pct:.1f}%\n"
            f"📋 Режим: Demo")

def fmt_close(symbol, direction, entry, exit_p, pnl, pnl_pct, reason, hold_h, balance):
    e = "✅" if pnl >= 0 else "❌"
    d = "Покупка" if direction == "long" else "Продажа"
    s = "+" if pnl >= 0 else ""
    h = f"{hold_h:.1f}ч" if hold_h >= 1 else f"{hold_h*60:.0f}мин"
    reasons = {
        "stop_loss": "🛑 Стоп-лосс", "tp1_partial": "🎯 Первая цель",
        "tp2_partial": "🎯 Вторая цель", "trailing_stop": "📉 Трейлинг-стоп",
        "peak_protection": "🛡️ Защита прибыли", "breakeven_stop": "🔒 Безубыток",
        "trailing_breakeven": "🔐 Скользящий безубыток",
    }
    r = reasons.get(reason, reason)
    return (f"{e} <b>Сделка закрыта: {symbol}</b>\n\n"
            f"📊 {d}: ${entry:,.2f} → ${exit_p:,.2f}\n"
            f"{'📈' if pnl>=0 else '📉'} Результат: {s}${pnl:,.2f} ({s}{pnl_pct:.2f}%)\n"
            f"⏱ Время: {h}\n📝 {r}\n💰 Баланс: ${balance:,.0f}")

def fmt_partial(symbol, pct, pnl, remaining, reason):
    e = "✅" if pnl >= 0 else "⚠️"
    s = "+" if pnl >= 0 else ""
    reasons = {"tp1_partial": "🎯 Первая цель", "tp2_partial": "🎯 Вторая цель"}
    r = reasons.get(reason, reason)
    return (f"{e} <b>Частичное закрытие {symbol}</b> ({pct}%)\n"
            f"{'📈' if pnl>=0 else '📉'} Результат: {s}${pnl:,.2f}\n"
            f"💰 Осталось: ${remaining:,.0f}\n📝 {r}")

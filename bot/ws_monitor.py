import asyncio
import json
import logging
import aiohttp
from datetime import datetime, timezone
from config import BYBIT_SYMBOL_MAP

logger = logging.getLogger("ws_monitor")

# Глобальный кэш цен — обновляется через WebSocket
_prices: dict[str, float] = {}
_last_update: dict[str, datetime] = {}

def get_cached_price(symbol: str) -> float | None:
    return _prices.get(symbol)

def get_all_prices() -> dict:
    return dict(_prices)

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"

def _bybit_sym(symbol: str) -> str:
    return BYBIT_SYMBOL_MAP.get(symbol, f"{symbol}USDT")

async def run_ws_price_monitor(symbols: list[str]):
    """
    WebSocket подключение к Bybit — получает цены в реальном времени.
    Обновляет глобальный кэш _prices каждый тик.
    """
    logger.info(f"WebSocket price monitor starting for {len(symbols)} symbols")

    # Подписываемся батчами по 10 символов
    topics = [f"tickers.{_bybit_sym(s)}" for s in symbols]

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    BYBIT_WS_URL,
                    heartbeat=20,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as ws:
                    logger.info("WebSocket connected to Bybit")

                    # Подписываемся на все тикеры
                    for i in range(0, len(topics), 10):
                        batch = topics[i:i+10]
                        await ws.send_json({
                            "op": "subscribe",
                            "args": batch
                        })
                        await asyncio.sleep(0.1)

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                if data.get("topic", "").startswith("tickers."):
                                    ticker_data = data.get("data", {})
                                    bybit_sym = data["topic"].replace("tickers.", "")

                                    # Обратный маппинг
                                    symbol = None
                                    for s in symbols:
                                        if _bybit_sym(s) == bybit_sym:
                                            symbol = s
                                            break

                                    if symbol and ticker_data.get("lastPrice"):
                                        price = float(ticker_data["lastPrice"])
                                        if price > 0:
                                            _prices[symbol] = price
                                            _last_update[symbol] = datetime.now(timezone.utc)

                            except (json.JSONDecodeError, KeyError, ValueError):
                                pass

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning(f"WebSocket error: {msg.data}")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"WebSocket disconnected: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)

async def run_fast_position_checker(check_callback):
    """
    Проверяет открытые позиции каждые 10 секунд используя WebSocket цены.
    check_callback — функция из trader.py которая проверяет выходы.
    """
    logger.info("Fast position checker started (10s interval)")
    await asyncio.sleep(30)  # ждём пока WebSocket подключится

    while True:
        try:
            if _prices:
                await check_callback(_prices)
        except Exception as e:
            logger.error(f"Fast position checker error: {e}")
        await asyncio.sleep(10)

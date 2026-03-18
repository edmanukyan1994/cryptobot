import asyncio
import json
import logging
import aiohttp
from datetime import datetime, timezone
from config import BYBIT_SYMBOL_MAP

logger = logging.getLogger("ws_monitor")

_prices: dict[str, float] = {}

def get_cached_price(symbol: str) -> float | None:
    return _prices.get(symbol)

def get_all_prices() -> dict:
    return dict(_prices)

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"

def _bybit_sym(symbol: str) -> str:
    return BYBIT_SYMBOL_MAP.get(symbol, f"{symbol}USDT")

async def save_prices_to_db(prices: dict):
    """Сохраняет цены из WebSocket напрямую в БД."""
    import db
    if not prices:
        return
    ts = datetime.now(timezone.utc)
    rows = [(sym, price, price, 0, 0, ts) for sym, price in prices.items()]
    try:
        await db.executemany(
            """INSERT INTO crypto_prices_bybit
               (symbol, price, mark_price, volume_24h, price_change_24h, ts)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            rows
        )
        logger.info(f"Prices saved: {len(rows)} symbols (WebSocket)")
    except Exception as e:
        logger.warning(f"Price save error: {e}")

async def run_ws_price_monitor(symbols: list[str]):
    logger.info(f"WebSocket price monitor starting for {len(symbols)} symbols")
    topics = [f"tickers.{_bybit_sym(s)}" for s in symbols]

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(BYBIT_WS_URL, heartbeat=20) as ws:
                    logger.info("WebSocket connected to Bybit")

                    for i in range(0, len(topics), 10):
                        await ws.send_json({"op": "subscribe", "args": topics[i:i+10]})
                        await asyncio.sleep(0.1)

                    save_tick = 0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                if data.get("topic", "").startswith("tickers."):
                                    ticker_data = data.get("data", {})
                                    bybit_sym = data["topic"].replace("tickers.", "")

                                    sym = None
                                    for s in symbols:
                                        if _bybit_sym(s) == bybit_sym:
                                            sym = s
                                            break

                                    if sym and ticker_data.get("lastPrice"):
                                        price = float(ticker_data["lastPrice"])
                                        if price > 0:
                                            _prices[sym] = price

                            except Exception:
                                pass

                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break

                        # Сохраняем в БД каждые 120 секунд
                        save_tick += 1
                        if save_tick >= 5000 and _prices:
                            await save_prices_to_db(dict(_prices))
                            save_tick = 0

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"WebSocket disconnected: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)

async def run_fast_position_checker(check_callback):
    logger.info("Fast position checker started (10s interval)")
    await asyncio.sleep(30)
    while True:
        try:
            if _prices:
                await check_callback(_prices)
        except Exception as e:
            logger.error(f"Fast position checker error: {e}")
        await asyncio.sleep(10)

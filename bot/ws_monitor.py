import asyncio
import json
import logging
import aiohttp
from datetime import datetime, timezone
from config import BYBIT_SYMBOL_MAP

logger = logging.getLogger("ws_monitor")

# Храним полный тикер по каждому символу
_tickers: dict[str, dict] = {}


def get_cached_price(symbol: str) -> float | None:
    item = _tickers.get(symbol)
    if not item:
        return None
    return item.get("price")


def get_all_prices() -> dict[str, float]:
    """
    Возвращает совместимый со старым кодом формат:
    {symbol: price}
    """
    return {
        symbol: float(item["price"])
        for symbol, item in _tickers.items()
        if item.get("price") is not None
    }


def get_all_tickers() -> dict[str, dict]:
    """
    Возвращает полный набор данных тикера.
    """
    return dict(_tickers)


BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"


def _bybit_sym(symbol: str) -> str:
    return BYBIT_SYMBOL_MAP.get(symbol, f"{symbol}USDT")


async def save_prices_to_db(tickers: dict[str, dict]):
    """
    Сохраняет полный тикер из WebSocket напрямую в БД.
    """
    import db

    if not tickers:
        return

    ts = datetime.now(timezone.utc)

    rows = []
    for sym, item in tickers.items():
        try:
            price = float(item.get("price") or 0)
            if price <= 0:
                continue

            rows.append(
                (
                    sym,
                    price,
                    float(item.get("mark_price") or price),
                    float(item.get("volume_24h") or 0),
                    float(item.get("price_change_24h") or 0),
                    float(item.get("turnover_24h") or 0),
                    float(item.get("high_price_24h") or 0),
                    float(item.get("low_price_24h") or 0),
                    float(item.get("prev_price_24h") or 0),
                    float(item.get("prev_price_1h") or 0),
                    float(item.get("open_interest") or 0),
                    float(item.get("open_interest_value") or 0),
                    float(item.get("funding_rate") or 0),
                    float(item.get("bid1_price") or 0),
                    float(item.get("bid1_size") or 0),
                    float(item.get("ask1_price") or 0),
                    float(item.get("ask1_size") or 0),
                    ts,
                )
            )
        except Exception:
            continue

    if not rows:
        return

    try:
        await db.executemany(
            """
            INSERT INTO crypto_prices_bybit
            (
                symbol,
                price,
                mark_price,
                volume_24h,
                price_change_24h,
                turnover_24h,
                high_price_24h,
                low_price_24h,
                prev_price_24h,
                prev_price_1h,
                open_interest,
                open_interest_value,
                funding_rate,
                bid1_price,
                bid1_size,
                ask1_price,
                ask1_size,
                ts
            )
            VALUES
            (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                $11,$12,$13,$14,$15,$16,$17,$18
            )
            """,
            rows,
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
                                        try:
                                            price = float(ticker_data.get("lastPrice") or 0)
                                            if price <= 0:
                                                continue

                                            _tickers[sym] = {
                                                "price": price,
                                                "mark_price": float(ticker_data.get("markPrice") or price),
                                                "volume_24h": float(ticker_data.get("volume24h") or 0),
                                                "price_change_24h": float(ticker_data.get("price24hPcnt") or 0),
                                                "turnover_24h": float(ticker_data.get("turnover24h") or 0),
                                                "high_price_24h": float(ticker_data.get("highPrice24h") or 0),
                                                "low_price_24h": float(ticker_data.get("lowPrice24h") or 0),
                                                "prev_price_24h": float(ticker_data.get("prevPrice24h") or 0),
                                                "prev_price_1h": float(ticker_data.get("prevPrice1h") or 0),
                                                "open_interest": float(ticker_data.get("openInterest") or 0),
                                                "open_interest_value": float(ticker_data.get("openInterestValue") or 0),
                                                "funding_rate": float(ticker_data.get("fundingRate") or 0),
                                                "bid1_price": float(ticker_data.get("bid1Price") or 0),
                                                "bid1_size": float(ticker_data.get("bid1Size") or 0),
                                                "ask1_price": float(ticker_data.get("ask1Price") or 0),
                                                "ask1_size": float(ticker_data.get("ask1Size") or 0),
                                            }
                                        except Exception:
                                            continue

                            except Exception:
                                pass

                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break

                        # Сохраняем в БД периодически
                        save_tick += 1
                        if save_tick >= 5000 and _tickers:
                            await save_prices_to_db(get_all_tickers())
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
            prices = get_all_prices()
            if prices:
                await check_callback(prices)
        except Exception as e:
            logger.error(f"Fast position checker error: {e}")

        await asyncio.sleep(10)

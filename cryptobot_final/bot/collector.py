import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from core.db import execute, executemany, fetchrow
from core.config import ALL_SYMBOLS, bybit_symbol

logger = logging.getLogger(__name__)

BYBIT_BASE = "https://api.bybit.com"


async def fetch_bybit_tickers(session: aiohttp.ClientSession) -> dict:
    """Fetch all linear futures tickers in one call."""
    url = f"{BYBIT_BASE}/v5/market/tickers"
    params = {"category": "linear"}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("retCode") != 0:
                logger.error(f"Bybit tickers error: {data.get('retMsg')}")
                return {}
            result = {}
            for item in data["result"]["list"]:
                sym = item["symbol"]
                if sym.endswith("USDT"):
                    base = sym.replace("USDT", "").replace("1000", "")
                    result[sym] = item
            return result
    except Exception as e:
        logger.error(f"fetch_bybit_tickers error: {e}")
        return {}


async def fetch_fear_greed(session: aiohttp.ClientSession) -> int | None:
    """Fetch Fear & Greed index from alternative.me"""
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            value = int(data["data"][0]["value"])
            classification = data["data"][0]["value_classification"]
            await execute(
                """INSERT INTO crypto_fear_greed (id, value, value_classification, updated_at)
                   VALUES ('latest', $1, $2, now())
                   ON CONFLICT (id) DO UPDATE SET value=$1, value_classification=$2, updated_at=now()""",
                value, classification
            )
            logger.info(f"Fear & Greed: {value} ({classification})")
            return value
    except Exception as e:
        logger.error(f"fetch_fear_greed error: {e}")
        return None


async def collect_prices(session: aiohttp.ClientSession):
    """Collect current prices for all symbols from Bybit."""
    tickers = await fetch_bybit_tickers(session)
    if not tickers:
        return

    rows = []
    now = datetime.now(timezone.utc)

    for symbol in ALL_SYMBOLS:
        bsym = bybit_symbol(symbol)
        ticker = tickers.get(bsym)
        if not ticker:
            continue

        try:
            price = float(ticker.get("lastPrice") or 0)
            if price <= 0:
                continue

            rows.append((
                symbol,
                price,
                float(ticker.get("markPrice") or price),
                float(ticker.get("indexPrice") or price),
                float(ticker.get("fundingRate") or 0),
                float(ticker.get("volume24h") or 0),
                float(ticker.get("price24hPcnt") or 0) * 100,
                float(ticker.get("bid1Price") or 0),
                float(ticker.get("ask1Price") or 0),
                now,
            ))
        except Exception as e:
            logger.warning(f"Price parse error {symbol}: {e}")

    if rows:
        await executemany(
            """INSERT INTO crypto_prices_bybit
               (symbol, price, mark_price, index_price, funding_rate,
                volume_24h, price_change_24h, bid1_price, ask1_price, ts)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
            rows
        )
        logger.info(f"Collected prices for {len(rows)} symbols")


async def get_latest_price(symbol: str) -> float | None:
    """Get most recent price for a symbol."""
    row = await fetchrow(
        """SELECT price FROM crypto_prices_bybit
           WHERE symbol = $1
           ORDER BY ts DESC LIMIT 1""",
        symbol
    )
    return float(row["price"]) if row else None


async def get_klines(session: aiohttp.ClientSession, symbol: str,
                     interval: str = "60", limit: int = 100) -> list[dict]:
    """Fetch OHLCV klines from Bybit."""
    bsym = bybit_symbol(symbol)
    url = f"{BYBIT_BASE}/v5/market/kline"
    params = {"category": "linear", "symbol": bsym, "interval": interval, "limit": limit}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("retCode") != 0:
                return []
            candles = []
            for item in data["result"]["list"]:
                candles.append({
                    "ts": int(item[0]),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                })
            return list(reversed(candles))
    except Exception as e:
        logger.error(f"get_klines {symbol} error: {e}")
        return []

import asyncio
import aiohttp
import json
import logging
from datetime import datetime, timezone
from config import PRICE_INTERVAL, BYBIT_SYMBOL_MAP
import db

logger = logging.getLogger("collector")

# Кэш цен из WebSocket (заполняется ws_monitor)
_ws_prices: dict = {}

def update_ws_price(symbol: str, price: float):
    """Вызывается из ws_monitor при получении новой цены."""
    _ws_prices[symbol] = price

async def get_active_symbols() -> list:
    rows = await db.fetch("SELECT symbol FROM crypto_assets WHERE is_active=true ORDER BY rank")
    return [r["symbol"] for r in rows]

async def fetch_fear_greed() -> dict | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                item = data["data"][0]
                return {"value": int(item["value"]), "label": item["value_classification"]}
    except Exception as e:
        logger.warning(f"Fear&Greed error: {e}")
        return None

async def run_price_collector():
    logger.info("Price collector started (WebSocket mode)")
    fg_tick = 0

    while True:
        try:
            if _ws_prices:
                ts = datetime.now(timezone.utc)
                rows = [(sym, price, price, 0, 0, ts) for sym, price in _ws_prices.items()]
                if rows:
                    await db.executemany(
                        """INSERT INTO crypto_prices_bybit
                           (symbol, price, mark_price, volume_24h, price_change_24h, ts)
                           VALUES ($1,$2,$3,$4,$5,$6)""",
                        rows
                    )
                    logger.info(f"Prices saved: {len(rows)} symbols (WebSocket)")

            fg_tick += 1
            if fg_tick >= 5:
                fg = await fetch_fear_greed()
                if fg:
                    await db.execute(
                        """INSERT INTO crypto_fear_greed (id, value, label, updated_at)
                           VALUES ('latest',$1,$2,now())
                           ON CONFLICT (id) DO UPDATE SET value=$1, label=$2, updated_at=now()""",
                        fg["value"], fg["label"]
                    )
                    logger.info(f"Fear&Greed: {fg['value']} ({fg['label']})")
                fg_tick = 0

        except Exception as e:
            logger.error(f"Collector error: {e}")

        await asyncio.sleep(PRICE_INTERVAL)

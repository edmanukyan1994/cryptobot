import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from config import bybit_symbol, PRICE_INTERVAL
import db

logger = logging.getLogger("collector")
BYBIT_BASE = "https://api.bybit.com"

async def fetch_tickers(session: aiohttp.ClientSession, symbols: list) -> dict:
    try:
        async with session.get(
            f"{BYBIT_BASE}/v5/market/tickers",
            params={"category": "linear"},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("retCode") != 0:
                return {}

            # Обратный маппинг bybit_symbol -> наш символ
            from config import BYBIT_SYMBOL_MAP
            reverse_map = {v: k for k, v in BYBIT_SYMBOL_MAP.items()}

            result = {}
            for item in data["result"]["list"]:
                ticker = item["symbol"]
                # Попробуем найти символ
                sym = reverse_map.get(ticker)
                if not sym:
                    if ticker.endswith("USDT") and not ticker.startswith("1000"):
                        sym = ticker[:-4]
                if sym and sym in symbols:
                    try:
                        result[sym] = {
                            "price": float(item["lastPrice"]),
                            "mark_price": float(item.get("markPrice") or item["lastPrice"]),
                            "volume_24h": float(item.get("volume24h") or 0),
                            "price_change_24h": float(item.get("price24hPcnt") or 0) * 100,
                        }
                    except (ValueError, KeyError):
                        pass
            return result
    except Exception as e:
        logger.warning(f"Bybit tickers error: {e}")
        return {}

async def fetch_fear_greed() -> dict | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                data = await resp.json()
                item = data["data"][0]
                return {"value": int(item["value"]), "label": item["value_classification"]}
    except Exception as e:
        logger.warning(f"Fear&Greed error: {e}")
        return None

async def get_active_symbols() -> list:
    rows = await db.fetch("SELECT symbol FROM crypto_assets WHERE is_active=true ORDER BY rank")
    return [r["symbol"] for r in rows]

async def run_price_collector():
    logger.info("Price collector started")
    fg_tick = 0

    headers = {"User-Agent": "Mozilla/5.0 (compatible; CryptoBot/1.0)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            try:
                symbols = await get_active_symbols()
                ts = datetime.now(timezone.utc)

                prices = await fetch_tickers(session, symbols)
                if prices:
                    rows = [
                        (sym, d["price"], d["mark_price"], d["volume_24h"], d["price_change_24h"], ts)
                        for sym, d in prices.items()
                    ]
                    await db.executemany(
                        """INSERT INTO crypto_prices_bybit
                           (symbol, price, mark_price, volume_24h, price_change_24h, ts)
                           VALUES ($1,$2,$3,$4,$5,$6)""",
                        rows
                    )
                    logger.info(f"Prices saved: {len(prices)} symbols")

                # Fear & Greed каждые 5 циклов (~10 мин)
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

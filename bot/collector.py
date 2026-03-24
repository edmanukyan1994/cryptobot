import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from config import PRICE_INTERVAL
import db
from ws_monitor import get_all_tickers

logger = logging.getLogger("collector")


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
            tickers = get_all_tickers()
            if tickers:
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

                if rows:
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
                        rows
                    )
                    logger.info(f"Prices saved: {len(rows)} symbols")
            else:
                logger.warning("No WebSocket prices yet")

            fg_tick += 1
            if fg_tick >= 5:
                fg = await fetch_fear_greed()
                if fg:
                    await db.execute(
                        """
                        INSERT INTO crypto_fear_greed (id, value, label, updated_at)
                        VALUES ('latest',$1,$2,now())
                        ON CONFLICT (id) DO UPDATE
                        SET value=$1, label=$2, updated_at=now()
                        """,
                        fg["value"], fg["label"]
                    )
                    logger.info(f"Fear&Greed: {fg['value']} ({fg['label']})")
                fg_tick = 0

        except Exception as e:
            logger.error(f"Collector error: {e}")

        await asyncio.sleep(PRICE_INTERVAL)

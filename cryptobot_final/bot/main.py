"""
CryptoBot Main — запускает все циклы асинхронно.
"""
import asyncio
import logging
import aiohttp
from datetime import datetime

from core.config import (
    CORE_SYMBOLS, ALL_SYMBOLS,
    CYCLE_INTERVAL, FEATURE_INTERVAL, FORECAST_INTERVAL,
    PRICE_INTERVAL, FEAR_GREED_INTERVAL, LOG_LEVEL
)
from core.db import get_pool, close_pool
from core.prices import collect_prices, fetch_fear_greed
from core.features import build_all_features
from core.forecasts import run_all_forecasts
from core.trading import run_trading_cycle
from core.telegram import send_message

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


async def price_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await collect_prices(session)
            except Exception as e:
                logger.error(f"Price loop error: {e}")
            await asyncio.sleep(PRICE_INTERVAL)


async def fear_greed_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await fetch_fear_greed(session)
            except Exception as e:
                logger.error(f"Fear&Greed loop error: {e}")
            await asyncio.sleep(FEAR_GREED_INTERVAL)


async def feature_loop():
    await asyncio.sleep(60)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await build_all_features(session, ALL_SYMBOLS)
            except Exception as e:
                logger.error(f"Feature loop error: {e}")
            await asyncio.sleep(FEATURE_INTERVAL)


async def forecast_loop():
    await asyncio.sleep(90)
    while True:
        try:
            await run_all_forecasts(CORE_SYMBOLS)
        except Exception as e:
            logger.error(f"Forecast loop error: {e}")
        await asyncio.sleep(FORECAST_INTERVAL)


async def trading_loop():
    await asyncio.sleep(120)
    while True:
        try:
            await run_trading_cycle(CORE_SYMBOLS)
        except Exception as e:
            logger.error(f"Trading loop error: {e}")
        await asyncio.sleep(CYCLE_INTERVAL)


async def main():
    logger.info("=" * 50)
    logger.info("CryptoBot starting...")
    logger.info(f"Symbols: {CORE_SYMBOLS}")
    logger.info("=" * 50)

    await get_pool()

    await send_message(
        "🤖 <b>CryptoBot запущен</b>\n\n"
        f"📊 Demo-режим активен\n"
        f"🪙 Символы: {', '.join(CORE_SYMBOLS)}\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )

    try:
        await asyncio.gather(
            price_loop(),
            fear_greed_loop(),
            feature_loop(),
            forecast_loop(),
            trading_loop(),
        )
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await close_pool()
        logger.info("Bye!")


if __name__ == "__main__":
    asyncio.run(main())

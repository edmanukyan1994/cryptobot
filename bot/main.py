import asyncio
import logging
import sys
from config import LOG_LEVEL, DEMO_MODE, DEMO_INITIAL_BALANCE
import db
from collector import run_price_collector, get_active_symbols
from features import run_features_builder
from forecaster import run_forecaster
from trader import run_trader, fast_exit_check
from ws_monitor import run_ws_price_monitor, run_fast_position_checker
from tg_commands import run_telegram_commands

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")

async def main():
    logger.info("=" * 40)
    logger.info("  CRYPTOBOT v1.1 — Python Edition")
    logger.info(f"  Mode: {'DEMO' if DEMO_MODE else 'LIVE'}")
    logger.info(f"  Balance: ${DEMO_INITIAL_BALANCE:,.0f}")
    logger.info("=" * 40)

    await db.get_pool()
    logger.info("Database connected")

    symbols = await get_active_symbols()
    logger.info(f"Loaded {len(symbols)} symbols")

    tasks = [
        asyncio.create_task(run_price_collector(), name="collector"),
        asyncio.create_task(run_features_builder(), name="features"),
        asyncio.create_task(run_forecaster(), name="forecaster"),
        asyncio.create_task(run_trader(), name="trader"),
        asyncio.create_task(run_ws_price_monitor(symbols), name="ws_monitor"),
        asyncio.create_task(run_fast_position_checker(fast_exit_check), name="fast_checker"),
        asyncio.create_task(run_telegram_commands(), name="tg_commands"),
    ]

    logger.info(f"Started {len(tasks)} modules")
    logger.info("WebSocket: 10s position checks active")
    logger.info("Telegram: /status /positions /balance /stats /closeall")

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        for task in tasks:
            task.cancel()
        await db.close_pool()
        logger.info("Bot stopped")

if __name__ == "__main__":
    asyncio.run(main())

import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://cryptobot:crypto123@postgres:5432/cryptobot")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
DEMO_INITIAL_BALANCE = float(os.getenv("DEMO_INITIAL_BALANCE", "10000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Интервалы (секунды)
PRICE_INTERVAL = 120      # цены каждые 2 минуты
FEATURE_INTERVAL = 600    # фичи каждые 10 минут
FORECAST_INTERVAL = 600   # прогнозы каждые 10 минут
TRADING_INTERVAL = 120    # торговый цикл каждые 2 минуты

# Маппинг символов для Bybit
BYBIT_SYMBOL_MAP = {
    "PEPE": "1000PEPEUSDT",
    "SHIB": "1000SHIBUSDT",
    "BONK": "1000BONKUSDT",
    "FLOKI": "1000FLOKIUSDT",
}

def bybit_symbol(sym: str) -> str:
    return BYBIT_SYMBOL_MAP.get(sym, f"{sym}USDT")

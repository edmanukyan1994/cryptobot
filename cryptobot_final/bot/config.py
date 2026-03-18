import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://cryptobot:password@localhost:5432/cryptobot")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
INITIAL_BALANCE = float(os.getenv("INITIAL_BALANCE", "10000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Trading cycle interval (seconds)
CYCLE_INTERVAL = 120       # run every 2 minutes
FEATURE_INTERVAL = 600     # rebuild features every 10 minutes
FORECAST_INTERVAL = 600    # run forecasts every 10 minutes
PRICE_INTERVAL = 30        # collect prices every 30 seconds
FEAR_GREED_INTERVAL = 3600 # update fear&greed hourly
SCORE_INTERVAL = 3600      # score forecasts hourly

# Symbols we trust based on data analysis
CORE_SYMBOLS = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA"]
ALL_SYMBOLS = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE",
    "LINK", "TRX", "MATIC", "UNI", "ATOM", "XLM",
    "NEAR", "APT", "ARB", "OP", "SUI", "PEPE",
    "INJ", "SHIB", "LTC", "FIL",
]

# Bybit symbol mapping (some have prefixes)
BYBIT_SYMBOL_MAP = {
    "PEPE": "1000PEPEUSDT",
    "SHIB": "1000SHIBUSDT",
    "BONK": "1000BONKUSDT",
    "FLOKI": "1000FLOKIUSDT",
}

def bybit_symbol(symbol: str) -> str:
    return BYBIT_SYMBOL_MAP.get(symbol, f"{symbol}USDT")

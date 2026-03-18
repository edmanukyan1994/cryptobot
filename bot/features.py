import asyncio
import aiohttp
import logging
import numpy as np
from datetime import datetime, timezone
from config import bybit_symbol, FEATURE_INTERVAL
import db

logger = logging.getLogger("features")
BYBIT_BASE = "https://api.bybit.com"

def calc_rsi(prices: list, period: int = 14):
    if len(prices) < period + 1:
        return None
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

def calc_ema(prices: list, period: int):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = float(np.mean(prices[:period]))
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_macd(prices: list):
    if len(prices) < 35:
        return None, None, None
    ema12 = calc_ema(prices, 12)
    ema26 = calc_ema(prices, 26)
    if ema12 is None or ema26 is None:
        return None, None, None
    macd_line = ema12 - ema26
    macd_vals = []
    for i in range(26, len(prices) + 1):
        e12 = calc_ema(prices[:i], 12)
        e26 = calc_ema(prices[:i], 26)
        if e12 and e26:
            macd_vals.append(e12 - e26)
    signal = calc_ema(macd_vals, 9) if len(macd_vals) >= 9 else None
    hist = round(macd_line - signal, 6) if signal else None
    return round(macd_line, 6), round(signal, 6) if signal else None, hist

def calc_bollinger(prices: list, period: int = 20):
    if len(prices) < period:
        return None, None, None, None
    recent = prices[-period:]
    mid = float(np.mean(recent))
    std = float(np.std(recent))
    upper = mid + 2 * std
    lower = mid - 2 * std
    width = ((upper - lower) / mid * 100) if mid > 0 else 0
    return round(upper, 6), round(mid, 6), round(lower, 6), round(width, 4)

def calc_atr(prices: list, period: int = 14):
    if len(prices) < period + 1:
        return None
    trs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    return round(float(np.mean(trs[-period:])), 8)

def detect_regime(rsi, r_24h, fear_greed):
    fg = fear_greed or 50
    if fg <= 15:
        return "crash"
    if fg <= 25:
        return "bearish"
    if fg >= 80:
        return "euphoria"
    if r_24h > 3:
        return "bullish"
    if r_24h < -3:
        return "bearish"
    if abs(r_24h) < 1.0:
        return "consolidation"
    return "neutral"

def calc_risk_score(rsi, fear_greed, atr_pct, r_24h):
    score = 50.0
    fg = fear_greed or 50
    if fg < 20: score += 20
    elif fg > 80: score += 10
    if atr_pct > 5: score += 20
    elif atr_pct > 2: score += 10
    if rsi and (rsi < 25 or rsi > 75): score += 10
    if abs(r_24h) > 10: score += 10
    return min(100, round(score, 1))

def find_sr(prices: list):
    if len(prices) < 20:
        return None, None, "neutral", 0.0
    current = prices[-1]
    supports, resistances = [], []
    for i in range(2, len(prices) - 2):
        if all(prices[i] < prices[i+j] for j in [-2,-1,1,2]):
            supports.append(prices[i])
        if all(prices[i] > prices[i+j] for j in [-2,-1,1,2]):
            resistances.append(prices[i])
    support = max([s for s in supports if s < current], default=None)
    resistance = min([r for r in resistances if r > current], default=None)
    signal, strength = "neutral", 0.0
    if support and (current - support) / current * 100 < 1.0:
        signal, strength = "bounce_support", 70.0
    elif resistance and (resistance - current) / current * 100 < 1.0:
        signal, strength = "bounce_resistance", 70.0
    elif resistance and current > resistance:
        signal, strength = "breakout_up", 50.0
    elif support and current < support:
        signal, strength = "breakout_down", 50.0
    return (round(support, 6) if support else None,
            round(resistance, 6) if resistance else None,
            signal, round(strength, 1))

async def fetch_klines(session, sym: str, interval="60", limit=50):
    try:
        bs = bybit_symbol(sym)
        async with session.get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "linear", "symbol": bs, "interval": interval, "limit": limit},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("retCode") != 0:
                return []
            return [{"open": r[1], "high": r[2], "low": r[3], "close": r[4]}
                    for r in reversed(data["result"]["list"])]
    except Exception:
        return []

def detect_candle(candles: list):
    if len(candles) < 3:
        return "none", 0.0
    c = candles[-1]
    o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
    body = abs(cl - o)
    rng = h - l
    if rng == 0:
        return "none", 0.0
    body_pct = body / rng
    if body_pct < 0.1:
        return "doji", 0.0
    lower_shadow = min(o, cl) - l
    upper_shadow = h - max(o, cl)
    if lower_shadow > body * 2 and upper_shadow < body * 0.5 and cl > o:
        return "hammer", 0.8
    if upper_shadow > body * 2 and lower_shadow < body * 0.5 and cl < o:
        return "shooting_star", -0.8
    prev = candles[-2]
    po, pc = float(prev["open"]), float(prev["close"])
    if cl < o and pc > po and o > pc and cl < po:
        return "bearish_engulfing", -0.9
    if cl > o and pc < po and o < pc and cl > po:
        return "bullish_engulfing", 0.9
    if cl > o and body_pct > 0.6:
        return "bullish_marubozu", 0.5
    if cl < o and body_pct > 0.6:
        return "bearish_marubozu", -0.5
    return "none", 0.0

async def build_features(session, symbol: str):
    price_rows = await db.fetch(
        "SELECT price FROM crypto_prices_bybit WHERE symbol=$1 ORDER BY ts DESC LIMIT 60",
        symbol
    )
    if len(price_rows) < 5:
        return None

    prices = [float(r["price"]) for r in reversed(price_rows)]
    current = prices[-1]

    latest = await db.fetchrow(
        "SELECT volume_24h, price_change_24h FROM crypto_prices_bybit WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
        symbol
    )
    if not latest:
        return None

    fg_row = await db.fetchrow("SELECT value FROM crypto_fear_greed WHERE id='latest'")
    fg = float(fg_row["value"]) if fg_row else 50.0

    global_row = await db.fetchrow("SELECT btc_dominance FROM crypto_market_global WHERE id='latest'")
    btc_dom = float(global_row["btc_dominance"]) if global_row else 55.0

    klines = await fetch_klines(session, symbol)
    rsi = calc_rsi(prices)
    macd, macd_sig, macd_hist = calc_macd(prices)
    bb_u, bb_m, bb_l, bb_w = calc_bollinger(prices)
    atr = calc_atr(prices)
    atr_pct = (atr / current * 100) if atr and current > 0 else 0

    r_24h = float(latest["price_change_24h"] or 0)
    r_1h = round((prices[-1] - prices[-2]) / prices[-2] * 100, 4) if len(prices) >= 2 and prices[-2] > 0 else 0

    candle_pattern, candle_score = detect_candle(klines) if klines else ("none", 0.0)

    kline_closes = [float(k["close"]) for k in klines] if klines else prices
    sup, res, sr_sig, sr_str = find_sr(kline_closes)

    regime = detect_regime(rsi, r_24h, fg)
    risk_score = calc_risk_score(rsi, fg, atr_pct, r_24h)

    return {
        "symbol": symbol, "ts": datetime.now(timezone.utc),
        "price": current, "volume_24h": float(latest["volume_24h"] or 0),
        "r_1h": r_1h, "r_24h": r_24h,
        "rsi_14": rsi, "macd": macd, "macd_signal": macd_sig, "macd_histogram": macd_hist,
        "bollinger_upper": bb_u, "bollinger_middle": bb_m, "bollinger_lower": bb_l, "bollinger_width": bb_w,
        "atr": atr, "fear_greed_index": fg, "btc_dominance": btc_dom,
        "regime": regime, "risk_score": risk_score,
        "candlestick_pattern": candle_pattern, "candlestick_score": candle_score,
        "support_1": sup, "resistance_1": res, "sr_signal": sr_sig, "sr_strength": sr_str,
    }

async def run_features_builder():
    logger.info("Features builder started")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                symbols = await db.fetch("SELECT symbol FROM crypto_assets WHERE is_active=true ORDER BY rank")
                built = 0
                for row in symbols:
                    sym = row["symbol"]
                    try:
                        f = await build_features(session, sym)
                        if f:
                            await db.execute(
                                """INSERT INTO crypto_features_hourly
                                   (symbol,ts,price,volume_24h,r_1h,r_24h,rsi_14,
                                    macd,macd_signal,macd_histogram,
                                    bollinger_upper,bollinger_middle,bollinger_lower,bollinger_width,
                                    atr,fear_greed_index,btc_dominance,regime,risk_score,
                                    candlestick_pattern,candlestick_score,
                                    support_1,resistance_1,sr_signal,sr_strength)
                                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                                           $11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25)""",
                                f["symbol"],f["ts"],f["price"],f["volume_24h"],f["r_1h"],f["r_24h"],
                                f["rsi_14"],f["macd"],f["macd_signal"],f["macd_histogram"],
                                f["bollinger_upper"],f["bollinger_middle"],f["bollinger_lower"],f["bollinger_width"],
                                f["atr"],f["fear_greed_index"],f["btc_dominance"],f["regime"],f["risk_score"],
                                f["candlestick_pattern"],f["candlestick_score"],
                                f["support_1"],f["resistance_1"],f["sr_signal"],f["sr_strength"],
                            )
                            built += 1
                    except Exception as e:
                        logger.warning(f"Features error {sym}: {e}")
                    await asyncio.sleep(0.3)
                logger.info(f"Features built: {built}/{len(symbols)}")
            except Exception as e:
                logger.error(f"Features builder error: {e}")
            await asyncio.sleep(FEATURE_INTERVAL)

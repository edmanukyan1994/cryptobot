"""
Feature builder — точная копия логики из build-crypto-features.
Считает RSI, MACD, Bollinger, ATR, S/R, свечные паттерны.
"""
import asyncio
import aiohttp
import logging
import numpy as np
from datetime import datetime, timezone
from core.db import fetch, execute, fetchrow
from core.prices import get_klines

logger = logging.getLogger(__name__)


# ── Индикаторы ──────────────────────────────────────────────

def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calc_ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def calc_macd(closes: list[float]):
    if len(closes) < 26:
        return None, None, None
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if not ema12 or not ema26:
        return None, None, None
    # Align lengths
    diff = len(ema12) - len(ema26)
    ema12 = ema12[diff:]
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal = calc_ema(macd_line, 9)
    if not signal:
        return None, None, None
    d = len(macd_line) - len(signal)
    macd_val = macd_line[-1]
    sig_val = signal[-1]
    hist = macd_val - sig_val
    return round(macd_val, 6), round(sig_val, 6), round(hist, 6)


def calc_bollinger(closes: list[float], period: int = 20, std_mult: float = 2.0):
    if len(closes) < period:
        return None, None, None, None
    window = closes[-period:]
    mid = np.mean(window)
    std = np.std(window)
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    width = (upper - lower) / mid * 100 if mid > 0 else 0
    return round(upper, 4), round(mid, 4), round(lower, 4), round(width, 4)


def calc_atr(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return None
    return round(np.mean(trs[-period:]), 6)


def calc_regime(price: float, bollinger_upper: float | None,
                bollinger_lower: float | None, rsi: float | None,
                fear_greed: int | None) -> str:
    score = 0
    if rsi is not None:
        if rsi > 60:
            score += 1
        elif rsi < 40:
            score -= 1
    if bollinger_upper and bollinger_lower:
        mid = (bollinger_upper + bollinger_lower) / 2
        if price > bollinger_upper:
            score += 2
        elif price < bollinger_lower:
            score -= 2
        elif price > mid:
            score += 1
        else:
            score -= 1
    if fear_greed is not None:
        if fear_greed < 20:
            return "crash"
        elif fear_greed > 75:
            score += 1
        elif fear_greed < 35:
            score -= 1
    if score >= 3:
        return "bullish"
    elif score <= -3:
        return "bearish"
    elif score >= 1:
        return "consolidation"
    else:
        return "neutral"


def calc_risk_score(rsi: float | None, fear_greed: int | None,
                    bollinger_width: float | None, r_24h: float | None) -> int:
    score = 50
    if rsi is not None:
        if rsi > 75 or rsi < 25:
            score += 15
        elif rsi > 65 or rsi < 35:
            score += 7
    if fear_greed is not None:
        if fear_greed < 15:
            score += 20
        elif fear_greed < 25:
            score += 10
        elif fear_greed > 80:
            score += 10
    if bollinger_width is not None:
        if bollinger_width > 10:
            score += 10
        elif bollinger_width > 6:
            score += 5
    if r_24h is not None:
        if abs(r_24h) > 10:
            score += 10
        elif abs(r_24h) > 5:
            score += 5
    return min(100, max(0, score))


# ── Свечные паттерны ─────────────────────────────────────────

def detect_candlestick_pattern(candles: list[dict]) -> tuple[str | None, float]:
    if len(candles) < 3:
        return None, 0.0

    c = candles[-1]
    p = candles[-2]
    pp = candles[-3]

    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    po, ph, pl, pc = p["open"], p["high"], p["low"], p["close"]
    body = abs(cl - o)
    full_range = h - l if h != l else 0.0001
    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l

    # Hammer (bullish)
    if lower_wick > body * 2 and upper_wick < body * 0.5 and cl > o:
        return "hammer", 0.7

    # Shooting star (bearish)
    if upper_wick > body * 2 and lower_wick < body * 0.5 and cl < o:
        return "shooting_star", -0.7

    # Bullish engulfing
    if (cl > o and pc < po and cl > po and o < pc):
        return "bullish_engulfing", 0.8

    # Bearish engulfing
    if (cl < o and pc > po and cl < po and o > pc):
        return "bearish_engulfing", -0.8

    # Doji
    if body / full_range < 0.1:
        return "doji", 0.0

    # Three white soldiers (bullish)
    ppo, ppf = pp["open"], pp["close"]
    if cl > o and pc > po and ppf > ppo and cl > pc > ppf:
        return "three_white_soldiers", 0.9

    # Three black crows (bearish)
    if cl < o and pc < po and ppf < ppo and cl < pc < ppf:
        return "three_black_crows", -0.9

    return None, 0.0


# ── S/R Levels ───────────────────────────────────────────────

def calc_support_resistance(candles: list[dict]) -> tuple[float | None, float | None, str | None, int]:
    if len(candles) < 20:
        return None, None, None, 0

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    current = candles[-1]["close"]

    # Find pivot highs and lows
    pivots_high = []
    pivots_low = []
    for i in range(2, len(candles) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1] and \
           highs[i] > highs[i-2] and highs[i] > highs[i+2]:
            pivots_high.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i+1] and \
           lows[i] < lows[i-2] and lows[i] < lows[i+2]:
            pivots_low.append(lows[i])

    # Find nearest support (below current)
    supports = sorted([p for p in pivots_low if p < current], reverse=True)
    resistances = sorted([p for p in pivots_high if p > current])

    support = supports[0] if supports else min(lows)
    resistance = resistances[0] if resistances else max(highs)

    # Determine SR signal
    dist_to_support = (current - support) / current * 100
    dist_to_resistance = (resistance - current) / current * 100

    sr_signal = None
    sr_strength = 0

    if dist_to_support < 1.0:
        sr_signal = "bounce_support"
        sr_strength = int(70 - dist_to_support * 30)
    elif dist_to_resistance < 1.0:
        sr_signal = "bounce_resistance"
        sr_strength = int(70 - dist_to_resistance * 30)
    elif current > resistance * 1.005:
        sr_signal = "breakout_up"
        sr_strength = min(80, int((current / resistance - 1) * 1000))
    elif current < support * 0.995:
        sr_signal = "breakout_down"
        sr_strength = min(80, int((1 - current / support) * 1000))

    return round(support, 4), round(resistance, 4), sr_signal, sr_strength


# ── Main feature builder ─────────────────────────────────────

async def build_features(session: aiohttp.ClientSession, symbol: str,
                          fear_greed: int | None, btc_dominance: float | None):
    """Build all features for a symbol and store in crypto_features_hourly."""

    # Get latest price
    price_row = await fetchrow(
        "SELECT price, volume_24h, price_change_24h, funding_rate FROM crypto_prices_bybit "
        "WHERE symbol=$1 ORDER BY ts DESC LIMIT 1", symbol
    )
    if not price_row:
        logger.warning(f"No price data for {symbol}")
        return

    price = float(price_row["price"])
    volume_24h = float(price_row["volume_24h"] or 0)
    r_24h = float(price_row["price_change_24h"] or 0)
    funding_rate = float(price_row["funding_rate"] or 0)

    # Get price history for indicators (last 50 prices)
    price_rows = await fetch(
        "SELECT price FROM crypto_prices_bybit WHERE symbol=$1 "
        "ORDER BY ts DESC LIMIT 50", symbol
    )
    closes = [float(r["price"]) for r in reversed(price_rows)]

    if len(closes) < 5:
        logger.warning(f"Not enough price history for {symbol}: {len(closes)}")
        return

    # Get klines for candlestick patterns and S/R
    candles = await get_klines(session, symbol, interval="60", limit=50)

    # Calculate indicators
    rsi = calc_rsi(closes)
    macd, macd_signal, macd_hist = calc_macd(closes)
    bb_upper, bb_mid, bb_lower, bb_width = calc_bollinger(closes)

    # 1h return
    r_1h = None
    if len(closes) >= 2:
        r_1h = round((closes[-1] / closes[-2] - 1) * 100, 4)

    # Candlestick pattern
    pattern, pattern_score = detect_candlestick_pattern(candles) if candles else (None, 0.0)

    # S/R levels
    support, resistance, sr_signal, sr_strength = calc_support_resistance(candles) if candles else (None, None, None, 0)

    # Regime and risk
    regime = calc_regime(price, bb_upper, bb_lower, rsi, fear_greed)
    risk_score = calc_risk_score(rsi, fear_greed, bb_width, r_24h)

    # BTC correlation (simplified — use r_1h correlation)
    correlation_btc = None  # calculated separately if needed

    now = datetime.now(timezone.utc)

    await execute(
        """INSERT INTO crypto_features_hourly
           (symbol, ts, price, volume_24h, r_1h, r_24h,
            rsi_14, macd, macd_signal, macd_histogram,
            bollinger_upper, bollinger_middle, bollinger_lower, bollinger_width,
            btc_dominance, fear_greed_index,
            regime, risk_score,
            candlestick_pattern, candlestick_score,
            support_1, resistance_1, sr_signal, sr_strength,
            funding_rate)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                   $17,$18,$19,$20,$21,$22,$23,$24,$25)
           ON CONFLICT (symbol, ts) DO UPDATE SET
           price=$3, volume_24h=$4, r_1h=$5, r_24h=$6,
           rsi_14=$7, macd=$8, macd_signal=$9, macd_histogram=$10,
           bollinger_upper=$11, bollinger_middle=$12, bollinger_lower=$13, bollinger_width=$14,
           btc_dominance=$15, fear_greed_index=$16,
           regime=$17, risk_score=$18,
           candlestick_pattern=$19, candlestick_score=$20,
           support_1=$21, resistance_1=$22, sr_signal=$23, sr_strength=$24,
           funding_rate=$25""",
        symbol, now, price, volume_24h, r_1h, r_24h,
        rsi, macd, macd_signal, macd_hist,
        bb_upper, bb_mid, bb_lower, bb_width,
        btc_dominance, fear_greed,
        regime, risk_score,
        pattern, pattern_score,
        support, resistance, sr_signal, sr_strength,
        funding_rate
    )
    logger.debug(f"Features built: {symbol} rsi={rsi} regime={regime} risk={risk_score}")


async def build_all_features(session: aiohttp.ClientSession, symbols: list[str]):
    """Build features for all symbols."""
    # Get global context
    fg_row = await fetchrow("SELECT value FROM crypto_fear_greed WHERE id='latest'")
    fear_greed = int(fg_row["value"]) if fg_row else 50

    mg_row = await fetchrow("SELECT btc_dominance FROM crypto_market_global WHERE id='latest'")
    btc_dominance = float(mg_row["btc_dominance"]) if mg_row else None

    logger.info(f"Building features for {len(symbols)} symbols (FG={fear_greed})")

    # Process in batches of 5 to avoid rate limits
    for i in range(0, len(symbols), 5):
        batch = symbols[i:i+5]
        tasks = [build_features(session, sym, fear_greed, btc_dominance) for sym in batch]
        await asyncio.gather(*tasks, return_exceptions=True)
        if i + 5 < len(symbols):
            await asyncio.sleep(1)

    logger.info("Features build complete")

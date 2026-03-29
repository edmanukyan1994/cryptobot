"""
Forecaster v2 — расширенная модель прогнозирования.
"""
import asyncio
import math
import logging
import json
from datetime import datetime, timezone
from config import FORECAST_INTERVAL
import db
from market_context import get_context

logger = logging.getLogger("forecaster")

HORIZONS = {"1h": 1, "4h": 4, "24h": 24}

BASE_WEIGHTS = {
    "momentum": 0.20,
    "rsi": 0.13,
    "sr": 0.13,
    "bollinger": 0.09,
    "macd": 0.09,
    "candlestick": 0.08,
    "fear_greed": 0.07,
    "regime": 0.03,
    "btc_trend": 0.08,
    "volume_trend": 0.05,
    "market_structure": 0.05,
}


def f(v):
    return float(v) if v is not None else None


async def get_weights(symbol: str, horizon: str) -> dict:
    rows = await db.fetch(
        "SELECT factor_name, current_weight FROM crypto_factor_weights WHERE symbol=$1 AND horizon=$2",
        symbol, horizon
    )
    if not rows:
        return {k: 1.0 for k in BASE_WEIGHTS}
    w = {r["factor_name"]: float(r["current_weight"]) for r in rows}
    for k in BASE_WEIGHTS:
        if k not in w:
            w[k] = 1.0
    return w


def score_momentum(r_1h, r_24h, regime):
    bull, bear = 0.0, 0.0
    if regime == "crash":
        if r_1h < 0:
            bear += 0.6
        if r_24h < -5:
            bear += 0.8
        return bull, bear
    if r_1h > 0.5:
        bull += 0.4
    elif r_1h < -0.5:
        bear += 0.4
    if r_24h > 3:
        bull += 0.8
    elif r_24h > 1:
        bull += 0.4
    elif r_24h < -3:
        bear += 0.8
    elif r_24h < -1:
        bear += 0.4
    return min(bull, 1.5), min(bear, 1.5)


def score_rsi(rsi, regime):
    if rsi is None:
        return 0.0, 0.0
    bull, bear = 0.0, 0.0
    if regime == "crash":
        if rsi < 25:
            bear += 0.9
        elif rsi < 35:
            bear += 0.5
        return bull, bear
    if rsi < 25:
        bull += 1.0
    elif rsi < 35:
        bull += 0.6
    elif rsi < 45:
        bull += 0.2
    elif rsi > 75:
        bear += 1.0
    elif rsi > 65:
        bear += 0.6
    elif rsi > 55:
        bear += 0.2
    return bull, bear


def score_bollinger(price, bb_upper, bb_lower):
    if not all([bb_upper, bb_lower]) or bb_upper == bb_lower:
        return 0.0, 0.0
    pos = (price - bb_lower) / (bb_upper - bb_lower)
    bull, bear = 0.0, 0.0
    if pos <= 0.1:
        bull += 1.0
    elif pos <= 0.25:
        bull += 0.5
    elif pos >= 0.9:
        bear += 1.0
    elif pos >= 0.75:
        bear += 0.5
    return bull, bear


def score_macd(macd, macd_sig, macd_hist):
    bull, bear = 0.0, 0.0
    if macd_hist is not None:
        if macd_hist > 0.001:
            bull += min(0.8, macd_hist * 100)
        elif macd_hist < -0.001:
            bear += min(0.8, abs(macd_hist) * 100)
    if macd is not None and macd_sig is not None:
        if macd > macd_sig:
            bull += 0.3
        elif macd < macd_sig:
            bear += 0.3
    return min(bull, 1.2), min(bear, 1.2)


def score_candlestick(score):
    if score > 0:
        return score, 0.0
    elif score < 0:
        return 0.0, abs(score)
    return 0.0, 0.0


def score_fear_greed(fg):
    if fg is None:
        return 0.0, 0.0
    bull, bear = 0.0, 0.0
    if fg <= 15:
        bull += 0.8
        bear += 0.4
    elif fg <= 25:
        bull += 0.4
        bear += 0.3
    elif fg >= 80:
        bear += 0.8
    elif fg >= 65:
        bear += 0.4
    return bull, bear


def score_regime(regime):
    m = {
        "bullish": (0.8, 0.0),
        "bearish": (0.0, 0.8),
        "crash": (0.0, 1.0),
        "oversold_crash": (0.2, 0.6),
        "euphoria": (0.0, 0.7),
        "consolidation": (0.1, 0.1),
        "neutral": (0.0, 0.0),
    }
    return m.get(regime, (0.0, 0.0))


def score_sr(price, support, resistance, sr_signal):
    bull, bear = 0.0, 0.0
    if sr_signal == "bounce_support":
        bull += 0.7
    elif sr_signal == "bounce_resistance":
        bear += 0.7
    elif sr_signal == "breakout_up":
        bull += 0.9
    elif sr_signal == "breakout_down":
        bear += 0.9
    if support and price > 0 and (price - support) / price * 100 < 1.0:
        bull += 0.4
    if resistance and price > 0 and (resistance - price) / price * 100 < 1.0:
        bear += 0.4
    return min(bull, 1.2), min(bear, 1.2)


def score_btc_trend(ctx: dict) -> tuple:
    bull, bear = 0.0, 0.0

    if not ctx:
        return 0.0, 0.0

    global_regime = ctx.get("global_regime", "neutral")
    above_ma200_1d = ctx.get("above_ma200_1d")
    above_ma50_1d = ctx.get("above_ma50_1d")
    above_ma20_4h = ctx.get("above_ma20_4h")
    btc_24h = ctx.get("btc_24h_change", 0)
    price_structure = ctx.get("price_structure_1d", "neutral")
    trend_strength = ctx.get("trend_strength", 0) / 100

    if global_regime == "bull_market":
        bull += 1.0 * trend_strength
    elif global_regime == "mild_bull":
        bull += 0.5 * trend_strength
    elif global_regime == "bear_market":
        bear += 1.0 * trend_strength
    elif global_regime == "mild_bear":
        bear += 0.5 * trend_strength

    if above_ma200_1d is True:
        bull += 0.5
    elif above_ma200_1d is False:
        bear += 0.5

    if above_ma50_1d is True:
        bull += 0.3
    elif above_ma50_1d is False:
        bear += 0.3

    if above_ma20_4h is True:
        bull += 0.2
    elif above_ma20_4h is False:
        bear += 0.2

    if price_structure == "uptrend":
        bull += 0.3
    elif price_structure == "downtrend":
        bear += 0.3

    if btc_24h > 3:
        bull += 0.4
    elif btc_24h > 1:
        bull += 0.2
    elif btc_24h < -3:
        bear += 0.4
    elif btc_24h < -1:
        bear += 0.2

    return min(bull, 1.5), min(bear, 1.5)


def score_volume_trend(ctx: dict, r_1h: float, r_24h: float) -> tuple:
    bull, bear = 0.0, 0.0

    if not ctx:
        return 0.0, 0.0

    vol_trend = ctx.get("vol_trend_1d", "neutral")

    if vol_trend == "increasing":
        if r_24h < -2:
            bear += 0.8
        elif r_24h > 2:
            bull += 0.8
        elif r_24h < 0:
            bear += 0.4
        elif r_24h > 0:
            bull += 0.4
    elif vol_trend == "decreasing":
        if r_24h < -2:
            bull += 0.3
        elif r_24h > 2:
            bear += 0.3

    return min(bull, 1.0), min(bear, 1.0)


def score_market_structure(ctx: dict) -> tuple:
    bull, bear = 0.0, 0.0

    if not ctx:
        return 0.0, 0.0

    structure_4h = ctx.get("price_structure_4h", "neutral")
    structure_1d = ctx.get("price_structure_1d", "neutral")

    if structure_4h == "uptrend":
        bull += 0.6
    elif structure_4h == "downtrend":
        bear += 0.6

    if structure_4h == structure_1d == "uptrend":
        bull += 0.4
    elif structure_4h == structure_1d == "downtrend":
        bear += 0.4
    elif structure_4h != structure_1d and "sideways" not in [structure_4h, structure_1d]:
        bull *= 0.7
        bear *= 0.7

    return min(bull, 1.0), min(bear, 1.0)


def calc_probability(bull, bear):
    diff = bull - bear
    total = bull + bear

    if total < 0.1:
        return "neutral", 50.0, 40.0

    if abs(diff) < 0.08:
        return "neutral", 50.0, 50.0

    direction = "up" if diff > 0 else "down"

    dominance = max(bull, bear) / total
    prob = 50 + (dominance - 0.5) * 100
    prob = max(50.0, min(100.0, prob))

    conf = max(50.0, min(100.0, dominance * 100))

    return direction, round(prob, 1), round(conf, 1)


def calc_corridor(price, atr, horizon_h, regime):
    if not atr or price <= 0:
        pcts = {1: 0.015, 4: 0.035, 24: 0.07}
        pct = pcts.get(horizon_h, 0.05)
        return round(price * (1 - pct), 6), round(price, 6), round(price * (1 + pct), 6)

    atr_pct = atr / price
    half = atr_pct * math.sqrt(horizon_h) * 0.25
    lm = 1.4 if regime in ("bearish", "crash") else (0.8 if regime == "bullish" else 1.0)
    um = 0.8 if regime in ("bearish", "crash") else (1.2 if regime == "bullish" else 1.0)
    return (
        round(price * (1 - half * lm), 6),
        round(price, 6),
        round(price * (1 + half * um), 6),
    )


async def forecast_symbol(symbol: str, features: dict) -> list:
    results = []
    ctx = get_context()

    for horizon, hours in HORIZONS.items():
        try:
            w = await get_weights(symbol, horizon)

            price = f(features.get("price")) or 0
            r_1h = f(features.get("r_1h")) or 0
            r_24h = f(features.get("r_24h")) or 0
            rsi = f(features.get("rsi_14"))
            macd = f(features.get("macd"))
            macd_sig = f(features.get("macd_signal"))
            macd_hist = f(features.get("macd_histogram"))
            bb_upper = f(features.get("bollinger_upper"))
            bb_lower = f(features.get("bollinger_lower"))
            fg = f(features.get("fear_greed_index"))
            atr = f(features.get("atr"))
            risk = f(features.get("risk_score")) or 50
            candle_sc = f(features.get("candlestick_score")) or 0
            support = f(features.get("support_1"))
            resistance = f(features.get("resistance_1"))
            sr_signal = features.get("sr_signal") or "neutral"
            regime = features.get("regime") or "neutral"

            fg_val = fg or 50
            btc_trend_mult = 0.8 if fg_val < 20 else (0.9 if fg_val < 35 else 1.0)
            vol_trend_mult = 0.9 if fg_val < 20 else 1.0

            scores = {
                "momentum": score_momentum(r_1h, r_24h, regime),
                "rsi": score_rsi(rsi, regime),
                "bollinger": score_bollinger(price, bb_upper, bb_lower),
                "macd": score_macd(macd, macd_sig, macd_hist),
                "candlestick": score_candlestick(candle_sc),
                "fear_greed": score_fear_greed(fg),
                "regime": score_regime(regime),
                "sr": score_sr(price, support, resistance, sr_signal),
                "btc_trend": tuple(x * btc_trend_mult for x in score_btc_trend(ctx)),
                "volume_trend": tuple(x * vol_trend_mult for x in score_volume_trend(ctx, r_1h, r_24h)),
                "market_structure": score_market_structure(ctx),
            }

            bull_total = sum(v[0] * BASE_WEIGHTS.get(k, 0.05) * w.get(k, 1.0) for k, v in scores.items())
            bear_total = sum(v[1] * BASE_WEIGHTS.get(k, 0.05) * w.get(k, 1.0) for k, v in scores.items())

            direction, prob, conf = calc_probability(bull_total, bear_total)
            p10, p50, p90 = calc_corridor(price, atr, hours, regime)

            results.append({
                "symbol": symbol,
                "horizon": horizon,
                "direction": direction,
                "direction_probability": prob,
                "confidence": conf,
                "risk_score": risk,
                "p10": p10,
                "p50": p50,
                "p90": p90,
                "regime": regime,
                "created_at": datetime.now(timezone.utc),
                "features_snapshot": {
                    "price": price,
                    "r_1h": r_1h,
                    "r_24h": r_24h,
                    "rsi_14": rsi,
                    "macd": macd,
                    "macd_signal": macd_sig,
                    "macd_histogram": macd_hist,
                    "bollinger_upper": bb_upper,
                    "bollinger_lower": bb_lower,
                    "fear_greed_index": fg,
                    "atr": atr,
                    "risk_score": risk,
                    "support_1": support,
                    "resistance_1": resistance,
                    "sr_signal": sr_signal,
                    "regime": regime,
                    "volume_24h": f(features.get("volume_24h")),
                    "btc_context": ctx,
                },
            })
        except Exception as e:
            logger.warning(f"Forecast error {symbol}/{horizon}: {e}")

    return results


async def get_latest_forecast(symbol: str, horizon: str = "4h"):
    row = await db.fetchrow(
        "SELECT * FROM crypto_forecast_runs WHERE symbol=$1 AND horizon=$2 ORDER BY created_at DESC LIMIT 1",
        symbol, horizon
    )
    return dict(row) if row else None


async def run_forecaster():
    logger.info("Forecaster started")
    while True:
        try:
            symbols = await db.fetch("SELECT symbol FROM crypto_assets WHERE is_active=true ORDER BY rank")
            count = 0
            for row in symbols:
                sym = row["symbol"]
                try:
                    f_row = await db.fetchrow(
                        "SELECT * FROM crypto_features_hourly WHERE symbol=$1 ORDER BY ts DESC LIMIT 1",
                        sym
                    )
                    if not f_row:
                        continue

                    forecasts = await forecast_symbol(sym, dict(f_row))
                    for fc in forecasts:
                        await db.execute(
                            """INSERT INTO crypto_forecast_runs
                               (symbol,horizon,direction,direction_probability,confidence,
                                risk_score,p10,p50,p90,regime,features_snapshot,created_at)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
                            fc["symbol"],
                            fc["horizon"],
                            fc["direction"],
                            fc["direction_probability"],
                            fc["confidence"],
                            fc["risk_score"],
                            fc["p10"],
                            fc["p50"],
                            fc["p90"],
                            fc["regime"],
                            json.dumps(fc["features_snapshot"]),
                            fc["created_at"],
                        )
                    count += 1
                except Exception as e:
                    logger.warning(f"Forecast {sym}: {e}")

                await asyncio.sleep(0.2)

            logger.info(f"Forecasts done: {count}/{len(symbols)}")
        except Exception as e:
            logger.error(f"Forecaster error: {e}")

        await asyncio.sleep(FORECAST_INTERVAL)

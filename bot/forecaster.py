"""
Forecaster v3 — расширенная модель прогнозирования.

Что добавлено:
1. market_mode / btc_regime / btc_structure_4h
2. relative_strength
3. impulse_score / reversal_score
4. volume_bucket / volatility_bucket
5. отдельная логика long impulse / long reversal / short impulse / trend
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
    "momentum":          0.16,
    "rsi":               0.11,
    "sr":                0.12,
    "bollinger":         0.07,
    "macd":              0.07,
    "candlestick":       0.06,
    "fear_greed":        0.05,
    "regime":            0.04,
    "btc_trend":         0.10,
    "volume_trend":      0.04,
    "market_structure":  0.07,
    "relative_strength": 0.05,
    "impulse":           0.03,
    "reversal":          0.03,
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


# ============================================================
# БАЗОВЫЕ ФАКТОРЫ
# ============================================================

def score_momentum(r_1h, r_24h, regime, market_mode):
    bull, bear = 0.0, 0.0

    # Жёсткий crash уже не означает "лонги запрещены навсегда"
    # Но при crash и bearish приоритет у шорта.
    if market_mode in ("bear", "bear_sideways") or regime in ("crash", "bearish"):
        if r_1h <= -0.03:
            bear += 0.8
        elif r_1h <= -0.01:
            bear += 0.4

        if r_24h <= -3:
            bear += 0.7
        elif r_24h <= -1:
            bear += 0.3

        # short squeeze / local rebound
        if r_1h >= 0.03 and r_24h <= -2:
            bull += 0.5
        elif r_1h >= 0.015 and r_24h <= -1:
            bull += 0.25

        return min(bull, 1.2), min(bear, 1.4)

    # bullish / neutral logic
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


def score_rsi(rsi, regime, market_mode):
    if rsi is None:
        return 0.0, 0.0

    bull, bear = 0.0, 0.0

    # В bear-режиме низкий RSI = не только медвежий, но и потенциальный reversal long
    if market_mode in ("bear", "bear_sideways") or regime in ("crash", "bearish"):
        if rsi < 20:
            bull += 0.55
            bear += 0.35
        elif rsi < 30:
            bull += 0.35
            bear += 0.25
        elif rsi > 70:
            bear += 0.6
        elif rsi > 62:
            bear += 0.35
        return bull, bear

    # Обычный режим
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


def score_fear_greed(fg, market_mode):
    if fg is None:
        return 0.0, 0.0

    bull, bear = 0.0, 0.0

    # В bear market extreme fear может давать отскок
    if fg <= 15:
        bull += 0.7
        if market_mode in ("bear", "bear_sideways"):
            bear += 0.2
    elif fg <= 25:
        bull += 0.35
        if market_mode in ("bear", "bear_sideways"):
            bear += 0.15
    elif fg >= 80:
        bear += 0.8
    elif fg >= 65:
        bear += 0.4

    return bull, bear


def score_regime(regime):
    m = {
        "bullish":        (0.8, 0.0),
        "bearish":        (0.0, 0.8),
        "crash":          (0.1, 0.9),
        "oversold_crash": (0.4, 0.6),
        "euphoria":       (0.0, 0.7),
        "consolidation":  (0.1, 0.1),
        "neutral":        (0.0, 0.0),
    }
    return m.get(regime, (0.0, 0.0))


def score_sr(price, support, resistance, sr_signal, dist_to_support_pct, dist_to_resistance_pct):
    bull, bear = 0.0, 0.0

    if sr_signal == "bounce_support":
        bull += 0.7
    elif sr_signal == "bounce_resistance":
        bear += 0.7
    elif sr_signal == "breakout_up":
        bull += 0.9
    elif sr_signal == "breakout_down":
        bear += 0.9

    if support and dist_to_support_pct is not None and dist_to_support_pct < 1.0:
        bull += 0.35
    if resistance and dist_to_resistance_pct is not None and dist_to_resistance_pct < 1.0:
        bear += 0.35

    return min(bull, 1.2), min(bear, 1.2)


# ============================================================
# НОВЫЕ ФАКТОРЫ
# ============================================================

def score_btc_trend(ctx: dict) -> tuple:
    bull, bear = 0.0, 0.0
    if not ctx:
        return 0.0, 0.0

    global_regime = ctx.get("global_regime", "neutral")
    market_mode = ctx.get("market_mode", "sideways")
    above_ma200_1d = ctx.get("above_ma200_1d")
    above_ma50_1d = ctx.get("above_ma50_1d")
    above_ma20_4h = ctx.get("above_ma20_4h")
    btc_24h = ctx.get("btc_24h_change", 0)
    price_structure_1d = ctx.get("price_structure_1d", "sideways")
    price_structure_4h = ctx.get("price_structure_4h", "sideways")
    trend_strength = (ctx.get("trend_strength", 0) or 0) / 100

    if global_regime == "bull_market":
        bull += 0.9 * max(0.5, trend_strength)
    elif global_regime == "mild_bull":
        bull += 0.45 * max(0.5, trend_strength)
    elif global_regime == "bear_market":
        bear += 0.9 * max(0.5, trend_strength)
    elif global_regime == "mild_bear":
        bear += 0.45 * max(0.5, trend_strength)
    elif global_regime == "crash":
        bear += 1.0

    if above_ma200_1d is True:
        bull += 0.45
    elif above_ma200_1d is False:
        bear += 0.45

    if above_ma50_1d is True:
        bull += 0.25
    elif above_ma50_1d is False:
        bear += 0.25

    if above_ma20_4h is True:
        bull += 0.15
    elif above_ma20_4h is False:
        bear += 0.15

    if price_structure_1d == "uptrend":
        bull += 0.25
    elif price_structure_1d == "downtrend":
        bear += 0.25

    if price_structure_4h == "uptrend":
        bull += 0.2
    elif price_structure_4h == "downtrend":
        bear += 0.2

    if btc_24h > 3:
        bull += 0.3
    elif btc_24h > 1:
        bull += 0.15
    elif btc_24h < -3:
        bear += 0.3
    elif btc_24h < -1:
        bear += 0.15

    # bear_sideways и bull_sideways пусть не такие жёсткие
    if market_mode == "bear_sideways":
        bull *= 0.85
    elif market_mode == "bull_sideways":
        bear *= 0.85

    return min(bull, 1.5), min(bear, 1.5)


def score_volume_trend(ctx: dict, r_1h: float, r_24h: float, volume_bucket: str) -> tuple:
    bull, bear = 0.0, 0.0
    if not ctx:
        return 0.0, 0.0

    vol_trend = ctx.get("vol_trend_1d", "neutral")

    # Объём самой монеты тоже учитываем
    volume_mult = 1.0
    if volume_bucket == "ultra":
        volume_mult = 1.1
    elif volume_bucket == "high":
        volume_mult = 1.0
    elif volume_bucket == "medium":
        volume_mult = 0.85
    elif volume_bucket == "low":
        volume_mult = 0.7
    elif volume_bucket == "trash":
        volume_mult = 0.4

    if vol_trend == "increasing":
        if r_24h < -2:
            bear += 0.7 * volume_mult
        elif r_24h > 2:
            bull += 0.7 * volume_mult
        elif r_24h < 0:
            bear += 0.35 * volume_mult
        elif r_24h > 0:
            bull += 0.35 * volume_mult

    elif vol_trend == "decreasing":
        if r_24h < -2 and r_1h > 0:
            bull += 0.25
        elif r_24h > 2 and r_1h < 0:
            bear += 0.25

    return min(bull, 1.0), min(bear, 1.0)


def score_market_structure(ctx: dict, market_mode: str) -> tuple:
    bull, bear = 0.0, 0.0
    if not ctx:
        return 0.0, 0.0

    structure_4h = ctx.get("price_structure_4h", "sideways")
    structure_1d = ctx.get("price_structure_1d", "sideways")

    if structure_4h == "uptrend":
        bull += 0.6
    elif structure_4h == "downtrend":
        bear += 0.6

    if structure_4h == structure_1d == "uptrend":
        bull += 0.35
    elif structure_4h == structure_1d == "downtrend":
        bear += 0.35
    elif structure_4h != structure_1d and "sideways" not in (structure_4h, structure_1d):
        bull *= 0.75
        bear *= 0.75

    if market_mode == "bear_sideways":
        bull *= 0.9
    elif market_mode == "bull_sideways":
        bear *= 0.9

    return min(bull, 1.0), min(bear, 1.0)


def score_relative_strength(relative_strength: float, btc_regime: str, market_mode: str) -> tuple:
    bull, bear = 0.0, 0.0

    if relative_strength is None:
        return 0.0, 0.0

    # Монета сильнее BTC → бычий признак
    if relative_strength >= 4:
        bull += 0.9
    elif relative_strength >= 2:
        bull += 0.55
    elif relative_strength >= 0.7:
        bull += 0.25

    # Монета слабее BTC → медвежий признак
    if relative_strength <= -4:
        bear += 0.9
    elif relative_strength <= -2:
        bear += 0.55
    elif relative_strength <= -0.7:
        bear += 0.25

    # В bear режимах относительная сила вверх особенно ценна для поиска long impulse
    if market_mode in ("bear", "bear_sideways") and relative_strength > 1.5:
        bull += 0.2

    return min(bull, 1.1), min(bear, 1.1)


def score_impulse(impulse_score: int, r_1h: float, market_mode: str, btc_momentum: str) -> tuple:
    bull, bear = 0.0, 0.0

    if impulse_score is None:
        return 0.0, 0.0

    if r_1h >= 0.03:
        bull += min(0.8, impulse_score * 0.16)
    elif r_1h <= -0.03:
        bear += min(0.8, impulse_score * 0.16)

    # Если рынок bearish, но локальный импульс вверх — не душим его полностью
    if market_mode in ("bear", "bear_sideways") and r_1h > 0.03:
        bull += 0.15

    if btc_momentum in ("strong_up", "weak_up") and r_1h > 0:
        bull += 0.1
    elif btc_momentum in ("strong_down", "weak_down") and r_1h < 0:
        bear += 0.1

    return min(bull, 0.9), min(bear, 0.9)


def score_reversal(reversal_score: int, rsi: float, sr_signal: str, market_mode: str, regime: str) -> tuple:
    bull, bear = 0.0, 0.0

    if reversal_score is None:
        return 0.0, 0.0

    # Long reversal особенно важен в bearish условиях
    if market_mode in ("bear", "bear_sideways") or regime in ("crash", "bearish"):
        if (rsi is not None and rsi <= 35) or sr_signal == "bounce_support":
            bull += min(0.9, reversal_score * 0.18)

    # Short reversal в bull market
    if market_mode in ("bull", "bull_sideways"):
        if (rsi is not None and rsi >= 65) or sr_signal == "bounce_resistance":
            bear += min(0.9, reversal_score * 0.18)

    # Нейтральный рынок
    if market_mode == "sideways":
        if rsi is not None and rsi <= 32:
            bull += min(0.5, reversal_score * 0.12)
        elif rsi is not None and rsi >= 68:
            bear += min(0.5, reversal_score * 0.12)

    return min(bull, 0.9), min(bear, 0.9)


# ============================================================
# ПРОБАБИЛИТИ
# ============================================================

def calc_probability(bull, bear):
    diff = bull - bear
    total = bull + bear

    if total < 0.1:
        return "neutral", 50.0, 40.0

    if abs(diff) < 0.06:
        return "neutral", 50.0, 50.0

    direction = "up" if diff > 0 else "down"
    dominance = max(bull, bear) / total

    prob = 50 + (dominance - 0.5) * 100
    prob = max(50.0, min(100.0, prob))

    # confidence чуть больше зависит от перевеса
    conf = 50 + min(50, abs(diff) * 22)
    conf = max(50.0, min(100.0, conf))

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


def enrich_setup_type(
    direction: str,
    market_mode: str,
    impulse_score: int,
    reversal_score: int,
    r_1h: float,
    r_24h: float,
    rsi: float | None,
    sr_signal: str,
    relative_strength: float | None,
):
    """
    Определяем тип сетапа уже на этапе forecast.
    trader потом сможет использовать это как готовую метку.
    """
    if direction == "down":
        if impulse_score >= 3 and r_1h <= -0.03:
            return "short_impulse"
        if market_mode in ("bear", "bear_sideways"):
            return "short_trend"
        return "short_trend"

    if direction == "up":
        # long reversal
        if (
            reversal_score >= 2
            and (
                (rsi is not None and rsi <= 35)
                or sr_signal == "bounce_support"
            )
        ):
            return "long_reversal"

        # long impulse
        if (
            impulse_score >= 3
            and r_1h >= 0.02
            and (relative_strength is not None and relative_strength >= 1.0)
        ):
            return "long_impulse"

        # long trend
        if market_mode in ("bull", "bull_sideways"):
            return "long_trend"

        return "long_trend"

    return "normal"


# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================

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

            # новые поля features
            btc_regime = str(features.get("btc_regime") or ctx.get("global_regime") or "neutral")
            btc_structure_4h = str(features.get("btc_structure_4h") or ctx.get("price_structure_4h") or "sideways")
            market_mode = str(features.get("market_mode") or ctx.get("market_mode") or "sideways")
            btc_momentum = str(features.get("btc_momentum") or ctx.get("momentum") or "flat")
            relative_strength = f(features.get("relative_strength")) or 0.0
            volume_bucket = str(features.get("volume_bucket") or "unknown")
            volatility_bucket = str(features.get("volatility_bucket") or "unknown")
            impulse_score = int(features.get("impulse_score") or 0)
            reversal_score = int(features.get("reversal_score") or 0)
            dist_to_support_pct = f(features.get("distance_to_support_pct"))
            dist_to_resistance_pct = f(features.get("distance_to_resistance_pct"))

            # мультипликаторы по среде
            fg_val = fg or 50
            vol_mult = 0.9 if fg_val < 20 else 1.0
            btc_mult = 0.85 if fg_val < 20 else 1.0

            # зашумленные low liquidity / extreme volatility режем по силе сигнала
            env_penalty = 1.0
            if volume_bucket == "trash":
                env_penalty *= 0.55
            elif volume_bucket == "low":
                env_penalty *= 0.8

            if volatility_bucket == "extreme":
                env_penalty *= 0.82

            scores = {
                "momentum": score_momentum(r_1h, r_24h, regime, market_mode),
                "rsi": score_rsi(rsi, regime, market_mode),
                "bollinger": score_bollinger(price, bb_upper, bb_lower),
                "macd": score_macd(macd, macd_sig, macd_hist),
                "candlestick": score_candlestick(candle_sc),
                "fear_greed": score_fear_greed(fg, market_mode),
                "regime": score_regime(regime),
                "sr": score_sr(price, support, resistance, sr_signal, dist_to_support_pct, dist_to_resistance_pct),
                "btc_trend": tuple(x * btc_mult for x in score_btc_trend(ctx)),
                "volume_trend": tuple(x * vol_mult for x in score_volume_trend(ctx, r_1h, r_24h, volume_bucket)),
                "market_structure": score_market_structure(ctx, market_mode),
                "relative_strength": score_relative_strength(relative_strength, btc_regime, market_mode),
                "impulse": score_impulse(impulse_score, r_1h, market_mode, btc_momentum),
                "reversal": score_reversal(reversal_score, rsi, sr_signal, market_mode, regime),
            }

            bull_total = sum(
                v[0] * BASE_WEIGHTS.get(k, 0.05) * w.get(k, 1.0)
                for k, v in scores.items()
            ) * env_penalty

            bear_total = sum(
                v[1] * BASE_WEIGHTS.get(k, 0.05) * w.get(k, 1.0)
                for k, v in scores.items()
            ) * env_penalty

            direction, prob, conf = calc_probability(bull_total, bear_total)

            # мягкая нормализация под market_mode
            if direction == "down" and market_mode == "bull":
                prob = max(50.0, prob - 6.0)
                conf = max(50.0, conf - 6.0)
            elif direction == "up" and market_mode == "bear":
                prob = max(50.0, prob - 4.0)
                conf = max(50.0, conf - 4.0)

            setup_type = enrich_setup_type(
                direction=direction,
                market_mode=market_mode,
                impulse_score=impulse_score,
                reversal_score=reversal_score,
                r_1h=r_1h,
                r_24h=r_24h,
                rsi=rsi,
                sr_signal=sr_signal,
                relative_strength=relative_strength,
            )

            p10, p50, p90 = calc_corridor(price, atr, hours, regime)

            results.append({
                "symbol": symbol,
                "horizon": horizon,
                "direction": direction,
                "direction_probability": round(prob, 1),
                "confidence": round(conf, 1),
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
                    "btc_regime": btc_regime,
                    "btc_structure_4h": btc_structure_4h,
                    "market_mode": market_mode,
                    "btc_momentum": btc_momentum,
                    "relative_strength": relative_strength,
                    "volume_bucket": volume_bucket,
                    "volatility_bucket": volatility_bucket,
                    "impulse_score": impulse_score,
                    "reversal_score": reversal_score,
                    "distance_to_support_pct": dist_to_support_pct,
                    "distance_to_resistance_pct": dist_to_resistance_pct,
                    "setup_type": setup_type,
                    "btc_context": ctx,
                    "factor_scores": {
                        k: {"bull": round(v[0], 4), "bear": round(v[1], 4)}
                        for k, v in scores.items()
                    },
                    "totals": {
                        "bull_total": round(bull_total, 4),
                        "bear_total": round(bear_total, 4),
                        "env_penalty": round(env_penalty, 4),
                    },
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
                            fc["created_at"]
                        )
                    count += 1

                except Exception as e:
                    logger.warning(f"Forecast {sym}: {e}")

                await asyncio.sleep(0.2)

            logger.info(f"Forecasts done: {count}/{len(symbols)}")

        except Exception as e:
            logger.error(f"Forecaster error: {e}")

        await asyncio.sleep(FORECAST_INTERVAL)

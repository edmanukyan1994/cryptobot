"""
Scoring Module v2 — переработанная система скоринга.

Ключевые изменения:
- Симметричная система для лонгов и шортов
- Фибоначчи как отдельный фактор
- FVG получает собственный вес в скоринге
- Упрощённые веса без distance (заменён на candle+fvg+fib)
- Порог входа 55 (жёстче — требует больше подтверждений)
"""

import logging
import json
from typing import Tuple, Optional
import db

logger = logging.getLogger("scoring")

_weights_cache = None
_weights_cache_time = None

DEFAULT_WEIGHTS = {
    "sr_signal": 0.30,
    "candle_confirmation": 0.25,
    "fvg_fibonacci": 0.15,
    "rsi": 0.12,
    "relative_strength": 0.10,
    "momentum_1h": 0.05,
    "volume": 0.03,
    "ml_signal": 0.00,
}


def _normalize_weights(raw: dict | None) -> dict:
    """Fill missing factors and sanitize invalid weight values."""
    merged = dict(DEFAULT_WEIGHTS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                merged[k] = float(v)
            except Exception:
                continue
    return merged


async def get_weights() -> dict:
    global _weights_cache, _weights_cache_time
    import time
    now = time.time()
    if _weights_cache is not None and _weights_cache_time is not None and (now - _weights_cache_time) < 60:
        return _weights_cache
    try:
        row = await db.fetchrow("SELECT weights, entry_threshold FROM crypto_scoring_weights WHERE id='current'")
        if row:
            import json as _json
            raw_weights = row["weights"]
            if isinstance(raw_weights, str):
                raw_weights = _json.loads(raw_weights)
            weights_float = _normalize_weights(raw_weights)
            try:
                threshold = int(row["entry_threshold"])
            except Exception:
                threshold = 45
            threshold = max(20, min(85, threshold))
            _weights_cache = {
                "weights": weights_float,
                "entry_threshold": threshold,
            }
            _weights_cache_time = now
            return _weights_cache
    except Exception as e:
        logger.warning(f"Failed to load scoring weights from DB: {e}")

    # Fallback — новые веса
    return {
        "weights": dict(DEFAULT_WEIGHTS),
        "entry_threshold": 45,
    }


async def get_entry_threshold() -> int:
    weights = await get_weights()
    return weights.get("entry_threshold", 45)


def _score_rsi(rsi: Optional[float], is_long: bool) -> int:
    """RSI — симметрично для лонга и шорта."""
    if rsi is None:
        return 0
    if is_long:
        if rsi <= 20:    return 100
        elif rsi <= 30:  return 75
        elif rsi <= 40:  return 40
        elif rsi <= 50:  return 10
        elif rsi >= 80:  return -60
        elif rsi >= 70:  return -30
        elif rsi >= 60:  return -10
    else:
        if rsi >= 80:    return 100
        elif rsi >= 70:  return 75
        elif rsi >= 60:  return 40
        elif rsi >= 50:  return 10
        elif rsi <= 20:  return -60
        elif rsi <= 30:  return -30
        elif rsi <= 40:  return -10
    return 0


def _score_momentum_1h(r_1h: Optional[float], is_long: bool) -> int:
    """Импульс за час — симметрично."""
    if r_1h is None:
        return 0
    if is_long:
        if r_1h >= 0.3:    return 80
        elif r_1h >= 0.1:  return 40
        elif r_1h >= 0.0:  return 10
        elif r_1h <= -0.5: return -60
        elif r_1h <= -0.2: return -30
        elif r_1h <= -0.1: return -10
    else:
        if r_1h <= -0.3:   return 80
        elif r_1h <= -0.1: return 40
        elif r_1h <= 0.0:  return 10
        elif r_1h >= 0.5:  return -60
        elif r_1h >= 0.2:  return -30
        elif r_1h >= 0.1:  return -10
    return 0


def _score_volume(volume_bucket: str) -> int:
    """Объём торгов."""
    if volume_bucket == "ultra":    return 100
    elif volume_bucket == "high":   return 75
    elif volume_bucket == "medium": return 50
    elif volume_bucket == "low":    return 20
    elif volume_bucket == "trash":  return -100
    return 0


def _score_sr_signal(sr_signal: str, is_long: bool) -> int:
    """SR сигнал — симметрично."""
    if is_long:
        if sr_signal == "bounce_support":                  return 100
        elif sr_signal == "breakout_up":                   return 80
        elif sr_signal == "retest_broken_resistance_long": return 60
        elif sr_signal == "neutral":                       return 0
        elif sr_signal == "breakout_down":                 return -80
        elif sr_signal == "bounce_resistance":             return -60
    else:
        if sr_signal == "bounce_resistance":               return 100
        elif sr_signal == "breakout_down":                 return 80
        elif sr_signal == "retest_broken_support_short":   return 60
        elif sr_signal == "neutral":                       return 0
        elif sr_signal == "breakout_up":                   return -80
        elif sr_signal == "bounce_support":                return -60
    return 0


def _score_relative_strength(rs: Optional[float], is_long: bool) -> int:
    """Относительная сила vs BTC — симметрично."""
    if rs is None:
        return 0
    if is_long:
        if rs >= 2.0:    return 100
        elif rs >= 1.0:  return 70
        elif rs >= 0.3:  return 30
        elif rs <= -2.0: return -60
        elif rs <= -1.0: return -40
        elif rs <= -0.3: return -20
    else:
        if rs <= -2.0:   return 100
        elif rs <= -1.0: return 70
        elif rs <= -0.3: return 30
        elif rs >= 2.0:  return -60
        elif rs >= 1.0:  return -40
        elif rs >= 0.3:  return -20
    return 0


def _score_fvg_fibonacci(features: dict, is_long: bool) -> int:
    """
    Комбинированный скор FVG + Фибоначчи.
    Объединяем в один фактор т.к. оба указывают на зоны дисбаланса/отката.
    Симметрично для лонга и шорта.
    """
    score = 0

    # FVG
    if is_long:
        if features.get("in_bullish_fvg"):
            score += 60
        elif (features.get("nearest_fvg") == "bullish" and
              (features.get("nearest_fvg_dist_pct") or 99) < 1.5):
            score += 30
        if features.get("in_bearish_fvg"):
            score -= 20
    else:
        if features.get("in_bearish_fvg"):
            score += 60
        elif (features.get("nearest_fvg") == "bearish" and
              (features.get("nearest_fvg_dist_pct") or 99) < 1.5):
            score += 30
        if features.get("in_bullish_fvg"):
            score -= 20

    # Фибоначчи
    fib_score = features.get("fib_score_long") if is_long else features.get("fib_score_short")
    if fib_score:
        # Конвертируем fib_score (-25..25) в 0..100 шкалу
        # fib_score_long: 0-25 (bullish retracement) или отрицательный (bearish)
        score += int(fib_score) * 2  # максимум +50 от Фибо

    return max(-100, min(100, score))


async def calculate_score(features: dict, direction: str, market_mode: str, ml_forecast: dict = None) -> int:
    is_long = direction == "long"
    weights_data = await get_weights()
    weights = weights_data["weights"]

    # Конвертируем asyncpg Record в dict
    if not isinstance(features, dict):
        try:
            features = dict(features)
        except Exception:
            pass

    def _f(v):
        try: return float(v) if v is not None else None
        except: return None

    rsi = _f(features.get("rsi_14"))
    r_1h = _f(features.get("r_1h"))
    volume_bucket = str(features.get("volume_bucket") or "low")
    sr_signal = str(features.get("sr_signal") or "neutral")
    rs = _f(features.get("relative_strength"))

    # Свечной скор (уже вычислен в features включая FVG+OB+MS)
    if is_long:
        candle_composite = int(features.get("candle_score_long") or 0)
    else:
        candle_composite = int(features.get("candle_score_short") or 0)

    # FVG + Fibonacci комбинированный скор
    fvg_fib_score = _score_fvg_fibonacci(features, is_long)

    scores = {
        "sr_signal":           _score_sr_signal(sr_signal, is_long),
        "candle_confirmation": candle_composite,
        "fvg_fibonacci":       fvg_fib_score,
        "rsi":                 _score_rsi(rsi, is_long),
        "relative_strength":   _score_relative_strength(rs, is_long),
        "momentum_1h":         _score_momentum_1h(r_1h, is_long),
        "volume":              _score_volume(volume_bucket),
    }

    # ML фактор
    ml_score = 0
    if ml_forecast:
        ml_dir = ml_forecast.get("direction")
        ml_prob = float(ml_forecast.get("direction_probability") or 0)
        if direction == "long" and ml_dir in ("up", "long"):
            ml_score = int(ml_prob * 0.2)
        elif direction == "short" and ml_dir in ("down", "short"):
            ml_score = int(ml_prob * 0.2)
        elif ml_dir and ml_dir not in ("neutral", None):
            ml_score = -15
    scores["ml_signal"] = ml_score

    # Базовый скор
    base_score = sum(float(score) * float(weights.get(factor, 0.0)) for factor, score in scores.items())

    # market_mode мультипликатор — симметрично
    if is_long:
        if market_mode == "bull":            mult = 1.2
        elif market_mode == "bull_sideways": mult = 1.1
        elif market_mode == "sideways":      mult = 1.0
        elif market_mode == "bear_sideways": mult = 0.9
        else:                                mult = 0.8
    else:
        if market_mode == "bear":            mult = 1.2
        elif market_mode == "bear_sideways": mult = 1.1
        elif market_mode == "sideways":      mult = 1.0
        elif market_mode == "bull_sideways": mult = 0.9
        else:                                mult = 0.8

    final_score = min(100, max(0, int(base_score * mult)))
    logger.debug(f"Score {direction}: {final_score} (base={base_score:.1f} mult={mult}) | {scores}")
    return final_score


async def should_enter_long(features: dict, forecast: dict, market_mode: str, ml_prediction: dict = None) -> tuple:
    if ml_prediction is None:
        from ml_client import get_ml_prediction
        ml_prediction = await get_ml_prediction(features)
    score = await calculate_score(features, "long", market_mode, ml_forecast=ml_prediction)
    threshold = await get_entry_threshold()
    weights = await get_weights()
    ml_weight = float(weights.get("weights", {}).get("ml_signal", 0.0))

    rsi = float(features.get("rsi_14")) if features.get("rsi_14") is not None else None
    dist = float(features.get("distance_to_support_pct")) if features.get("distance_to_support_pct") is not None else None
    volume_bucket = features.get("volume_bucket") or "low"

    # Экстремальная перепроданность у уровня — приоритетный вход
    if rsi is not None and rsi <= 20 and dist is not None and dist <= 0.5:
        if volume_bucket not in ("trash", "low"):
            return True, score, f"extreme_oversold_rsi={rsi:.1f}_dist={dist:.2f}"

    ml_dir = (ml_prediction or {}).get("direction")
    ml_prob = float((ml_prediction or {}).get("direction_probability") or 0)

    # Если ML-вес отключён, не блокируем вход направлением ML.
    if ml_weight <= 0:
        if score >= threshold:
            return True, score, f"score={score}_th={threshold}_ml_off"
        return False, score, f"score={score}<{threshold}_ml_off"

    if ml_dir in ("up", "neutral", None):
        if score >= threshold:
            return True, score, f"score={score}_ml={ml_dir}({ml_prob:.0f})"
        elif score >= threshold - 8 and ml_prob >= 65:
            return True, score, f"score={score}_ml_boost"

    return False, score, f"score={score}<{threshold}_or_dir={ml_dir}"


async def should_enter_short(features: dict, forecast: dict, market_mode: str, ml_prediction: dict = None) -> tuple:
    if ml_prediction is None:
        from ml_client import get_ml_prediction
        ml_prediction = await get_ml_prediction(features)
    score = await calculate_score(features, "short", market_mode, ml_forecast=ml_prediction)
    threshold = await get_entry_threshold()
    weights = await get_weights()
    ml_weight = float(weights.get("weights", {}).get("ml_signal", 0.0))

    rsi = float(features.get("rsi_14")) if features.get("rsi_14") is not None else None
    dist = float(features.get("distance_to_resistance_pct")) if features.get("distance_to_resistance_pct") is not None else None
    volume_bucket = features.get("volume_bucket") or "low"

    # Экстремальная перекупленность у уровня — приоритетный вход
    if rsi is not None and rsi >= 80 and dist is not None and dist <= 0.5:
        if volume_bucket not in ("trash", "low"):
            return True, score, f"extreme_overbought_rsi={rsi:.1f}_dist={dist:.2f}"

    ml_dir = (ml_prediction or {}).get("direction")
    ml_prob = float((ml_prediction or {}).get("direction_probability") or 0)

    # Если ML-вес отключён, не блокируем вход направлением ML.
    if ml_weight <= 0:
        if score >= threshold:
            return True, score, f"score={score}_th={threshold}_ml_off"
        return False, score, f"score={score}<{threshold}_ml_off"

    if ml_dir in ("down", "neutral", None):
        if score >= threshold:
            return True, score, f"score={score}_ml={ml_dir}({ml_prob:.0f})"
        elif score >= threshold - 8 and ml_prob >= 65:
            return True, score, f"score={score}_ml_boost"

    return False, score, f"score={score}<{threshold}_or_dir={ml_dir}"


async def should_enter(features: dict, forecast: dict, market_mode: str, direction: str, ml_prediction: dict = None) -> tuple:
    if direction == "long":
        return await should_enter_long(features, forecast, market_mode, ml_prediction=ml_prediction)
    elif direction == "short":
        return await should_enter_short(features, forecast, market_mode, ml_prediction=ml_prediction)
    return False, 0, f"invalid_direction={direction}"

"""
Scoring Module — система весов для принятия торговых решений.

Веса хранятся в БД в таблице crypto_scoring_weights.
"""

import logging
import json
from typing import Tuple, Optional
import db

logger = logging.getLogger("scoring")

# Кэш для весов (обновляется раз в минуту)
_weights_cache = None
_weights_cache_time = None


async def get_weights() -> dict:
    """Загружает веса из БД с кэшированием."""
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
            # weights может прийти как строка JSON или как dict
            if isinstance(raw_weights, str):
                raw_weights = _json.loads(raw_weights)
            weights_float = {k: float(v) for k, v in raw_weights.items()}
            _weights_cache = {
                "weights": weights_float,
                "entry_threshold": int(row["entry_threshold"]),
            }
            _weights_cache_time = now
            return _weights_cache
    except Exception as e:
        logger.warning(f"Failed to load scoring weights from DB: {e}")
    
    # Fallback веса (добавлен ml_signal)
    return {
        "weights": {
            "distance": 0.20,
            "rsi": 0.10,
            "momentum_1h": 0.15,
            "momentum_24h": 0.10,
            "volume": 0.10,
            "sr_signal": 0.15,
            "relative_strength": 0.10,
            "market_mode": 0.10,
            "ml_signal": 0.15,
        },
        "entry_threshold": 50,
    }


async def get_entry_threshold() -> int:
    """Возвращает порог входа из БД."""
    weights = await get_weights()
    return weights.get("entry_threshold", 50)


def _score_distance(dist: Optional[float], is_long: bool) -> int:
    """Оценивает расстояние до уровня (0-100)."""
    if dist is None:
        return 0
    if dist <= 0.5:  return 100
    elif dist <= 1.0: return 75
    elif dist <= 2.0: return 50
    elif dist <= 3.0: return 25
    return 0


def _score_rsi(rsi: Optional[float], is_long: bool) -> int:
    """Оценивает RSI (0-100)."""
    if rsi is None:
        return 0
    if is_long:
        if rsi <= 20:   return 100
        elif rsi <= 30: return 70
        elif rsi <= 40: return 30
        elif rsi >= 80: return -50
        elif rsi >= 70: return -30
    else:
        if rsi >= 80:   return 100
        elif rsi >= 70: return 70
        elif rsi >= 60: return 30
        elif rsi <= 20: return -50
        elif rsi <= 30: return -30
    return 0


def _score_momentum_1h(r_1h: Optional[float], is_long: bool) -> int:
    """Оценивает импульс за час (0-100)."""
    if r_1h is None:
        return 0
    if is_long:
        if r_1h >= 0.5:   return 100
        elif r_1h >= 0.2: return 70
        elif r_1h >= 0.05: return 30
        elif r_1h <= -0.5: return -70
        elif r_1h <= -0.2: return -30
    else:
        if r_1h <= -0.5:   return 100
        elif r_1h <= -0.2: return 70
        elif r_1h <= -0.05: return 30
        elif r_1h >= 0.5:  return -70
        elif r_1h >= 0.2:  return -30
    return 0


def _score_momentum_24h(r_24h: Optional[float], is_long: bool) -> int:
    """Оценивает импульс за 24 часа (0-100)."""
    if r_24h is None:
        return 0
    if is_long:
        if r_24h >= 2.0:   return 100
        elif r_24h >= 1.0: return 50
        elif r_24h <= -3.0: return -100
        elif r_24h <= -2.0: return -50
    else:
        if r_24h <= -2.0:  return 100
        elif r_24h <= -1.0: return 50
        elif r_24h >= 3.0:  return -100
        elif r_24h >= 2.0:  return -50
    return 0


def _score_volume(volume_bucket: str) -> int:
    """Оценивает объём торгов (0-100)."""
    if volume_bucket == "ultra":   return 100
    elif volume_bucket == "high":  return 75
    elif volume_bucket == "medium": return 50
    elif volume_bucket == "low":   return 20
    elif volume_bucket == "trash": return -100
    return 0


def _score_sr_signal(sr_signal: str, is_long: bool) -> int:
    """Оценивает S/R сигнал (0-100)."""
    if is_long:
        if sr_signal == "bounce_support":                return 100
        elif sr_signal == "breakout_up":                 return 80
        elif sr_signal == "retest_broken_resistance_long": return 60
        elif sr_signal == "breakout_down":               return -80
        elif sr_signal == "bounce_resistance":           return -50
    else:
        if sr_signal == "bounce_resistance":             return 100
        elif sr_signal == "breakout_down":               return 80
        elif sr_signal == "retest_broken_support_short": return 60
        elif sr_signal == "breakout_up":                 return -80
        elif sr_signal == "bounce_support":              return -50
    return 0


def _score_relative_strength(rs: Optional[float], is_long: bool) -> int:
    """Оценивает относительную силу к BTC (0-100)."""
    if rs is None:
        return 0
    if is_long:
        if rs >= 2.0:   return 100
        elif rs >= 1.0: return 70
        elif rs >= 0.5: return 30
        elif rs <= -1.0: return -50
        elif rs <= -0.5: return -30
    else:
        if rs <= -2.0:  return 100
        elif rs <= -1.0: return 70
        elif rs <= -0.5: return 30
        elif rs >= 1.0:  return -50
        elif rs >= 0.5:  return -30
    return 0


def _score_market_mode(market_mode: str, is_long: bool) -> int:
    """Оценивает режим рынка (0-100). Только бонус, без штрафа."""
    if is_long:
        if market_mode == "bull":            return 100
        elif market_mode == "bull_sideways": return 70
        elif market_mode == "sideways":      return 40
        elif market_mode == "bear_sideways": return 10
        elif market_mode == "bear":          return 0
    else:
        if market_mode == "bear":            return 100
        elif market_mode == "bear_sideways": return 70
        elif market_mode == "sideways":      return 40
        elif market_mode == "bull_sideways": return 10
        elif market_mode == "bull":          return 0
    return 0



def _score_candle_confirmation(candle_pattern: str, candle_score: float, sr_signal: str, is_long: bool) -> int:
    """Упрощённая оценка — используем candle_score_long/short из features."""
    if not candle_pattern or candle_pattern == "none":
        return 0
    if is_long:
        if candle_pattern in ("rejection_low", "hammer", "inverted_hammer"):
            return 100 if sr_signal == "bounce_support" else 60
        elif candle_pattern in ("bullish_engulfing", "bullish_marubozu"):
            return 50
        elif candle_pattern in ("rejection_high", "shooting_star", "bearish_engulfing"):
            return -50
    else:
        if candle_pattern in ("rejection_high", "shooting_star", "hanging_man"):
            return 100 if sr_signal == "bounce_resistance" else 60
        elif candle_pattern in ("bearish_engulfing", "bearish_marubozu"):
            return 50
        elif candle_pattern in ("rejection_low", "hammer", "bullish_engulfing"):
            return -50
    return 0



async def calculate_score(features: dict, direction: str, market_mode: str, ml_forecast: dict = None) -> int:
    is_long = direction == "long"
    weights_data = await get_weights()
    weights = weights_data["weights"]

    # Конвертируем asyncpg Record в dict если нужно
    if not isinstance(features, dict):
        try:
            features = dict(features)
        except Exception:
            pass

    def _f(v):
        try: return float(v) if v is not None else None
        except: return None

    dist = _f(features.get("distance_to_support_pct") if is_long else features.get("distance_to_resistance_pct"))
    rsi = _f(features.get("rsi_14"))
    r_1h = _f(features.get("r_1h"))
    r_24h = _f(features.get("r_24h"))
    volume_bucket = features.get("volume_bucket") or "low"
    sr_signal = features.get("sr_signal") or "neutral"
    rs = _f(features.get("relative_strength"))

    candle_pattern = str(features.get("candlestick_pattern") or "none")
    candle_score_val = float(features.get("candlestick_score") or 0)

    # Получаем готовый свечной скор из features (включает FVG + OB + MS)
    if is_long:
        candle_composite = int(features.get("candle_score_long") or
            _score_candle_confirmation(candle_pattern, candle_score_val, sr_signal, is_long))
    else:
        candle_composite = int(features.get("candle_score_short") or
            _score_candle_confirmation(candle_pattern, candle_score_val, sr_signal, is_long))

    scores = {
        "sr_signal":            _score_sr_signal(sr_signal, is_long),
        "candle_confirmation":  candle_composite,
        "momentum_1h":          _score_momentum_1h(r_1h, is_long),
        "rsi":                  _score_rsi(rsi, is_long),
        "relative_strength":    _score_relative_strength(rs, is_long),
        "volume":               _score_volume(volume_bucket),
        "momentum_24h":         _score_momentum_24h(r_24h, is_long),
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

    # Базовый скор (без market_mode)
    base_score = sum(float(score) * float(weights.get(factor, 0.0)) for factor, score in scores.items())

    # market_mode как мультипликатор
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
    logger.debug(f"Score {direction}: {final_score} (base={base_score:.1f} mult={mult}) | factors={scores}")
    return final_score


async def should_enter_long(features: dict, forecast: dict, market_mode: str) -> tuple:
    from ml_client import get_ml_prediction
    ml_prediction = await get_ml_prediction(features)
    score = await calculate_score(features, "long", market_mode, ml_forecast=ml_prediction)
    threshold = await get_entry_threshold()

    rsi = float(features.get("rsi_14")) if features.get("rsi_14") is not None else None
    dist = float(features.get("distance_to_support_pct")) if features.get("distance_to_support_pct") is not None else None
    volume_bucket = features.get("volume_bucket") or "low"

    if rsi is not None and rsi <= 20 and dist is not None and dist <= 0.5:
        if volume_bucket not in ("trash", "low"):
            return True, score, f"extreme_oversold_rsi={rsi:.1f}_dist={dist:.2f}"

    ml_dir = (ml_prediction or {}).get("direction")
    ml_prob = float((ml_prediction or {}).get("direction_probability") or 0)

    if ml_dir in ("up", "neutral", None):
        if score >= threshold:
            return True, score, f"score={score}_ml={ml_dir}({ml_prob:.0f})"
        elif score >= threshold - 10 and ml_prob >= 60:
            return True, score, f"score={score}_ml_boost"

    return False, score, f"score={score}<{threshold}_or_dir={ml_dir}"


async def should_enter_short(features: dict, forecast: dict, market_mode: str) -> tuple:
    from ml_client import get_ml_prediction
    ml_prediction = await get_ml_prediction(features)
    score = await calculate_score(features, "short", market_mode, ml_forecast=ml_prediction)
    threshold = await get_entry_threshold()

    rsi = float(features.get("rsi_14")) if features.get("rsi_14") is not None else None
    dist = float(features.get("distance_to_resistance_pct")) if features.get("distance_to_resistance_pct") is not None else None
    volume_bucket = features.get("volume_bucket") or "low"

    if rsi is not None and rsi >= 80 and dist is not None and dist <= 0.5:
        if volume_bucket not in ("trash", "low"):
            return True, score, f"extreme_overbought_rsi={rsi:.1f}_dist={dist:.2f}"

    ml_dir = (ml_prediction or {}).get("direction")
    ml_prob = float((ml_prediction or {}).get("direction_probability") or 0)


    if ml_dir in ("down", "neutral", None):
        if score >= threshold:
            return True, score, f"score={score}_ml={ml_dir}({ml_prob:.0f})"
        elif score >= threshold - 10 and ml_prob >= 60:
            return True, score, f"score={score}_ml_boost"

    return False, score, f"score={score}<{threshold}_or_dir={ml_dir}"


async def should_enter(features: dict, forecast: dict, market_mode: str, direction: str) -> tuple:
    if direction == "long":
        return await should_enter_long(features, forecast, market_mode)
    elif direction == "short":
        return await should_enter_short(features, forecast, market_mode)
    return False, 0, f"invalid_direction={direction}"

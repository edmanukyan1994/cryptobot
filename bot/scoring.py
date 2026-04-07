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


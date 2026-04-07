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
    """Оценивает расстояние до уровня."""
    if dist is None:
        return 0
    
    if dist <= 0.5:
        return 20
    elif dist <= 1.0:
        return 15
    elif dist <= 2.0:
        return 10
    elif dist <= 3.0:
        return 5
    return 0


def _score_rsi(rsi: Optional[float], is_long: bool) -> int:
    """Оценивает RSI."""
    if rsi is None:
        return 0
    
    if is_long:
        if rsi <= 20:
            return 10
        elif rsi <= 30:
            return 7
        elif rsi <= 40:
            return 3
        elif rsi >= 80:
            return -5
        elif rsi >= 70:
            return -3
    else:
        if rsi >= 80:
            return 10
        elif rsi >= 70:
            return 7
        elif rsi >= 60:
            return 3
        elif rsi <= 20:
            return -5
        elif rsi <= 30:
            return -3
    return 0


def _score_momentum_1h(r_1h: Optional[float], is_long: bool) -> int:
    """Оценивает импульс за час."""
    if r_1h is None:
        return 0
    
    if is_long:
        if r_1h >= 0.5:
            return 15
        elif r_1h >= 0.2:
            return 10
        elif r_1h >= 0.05:
            return 5
        elif r_1h <= -0.5:
            return -10
        elif r_1h <= -0.2:
            return -5
    else:
        if r_1h <= -0.5:
            return 15
        elif r_1h <= -0.2:
            return 10
        elif r_1h <= -0.05:
            return 5
        elif r_1h >= 0.5:
            return -10
        elif r_1h >= 0.2:
            return -5
    return 0


def _score_momentum_24h(r_24h: Optional[float], is_long: bool) -> int:
    """Оценивает импульс за 24 часа."""
    if r_24h is None:
        return 0
    
    if is_long:
        if r_24h >= 2.0:
            return 10
        elif r_24h >= 1.0:
            return 5
        elif r_24h <= -3.0:
            return -10
        elif r_24h <= -2.0:
            return -5
    else:
        if r_24h <= -2.0:
            return 10
        elif r_24h <= -1.0:
            return 5
        elif r_24h >= 3.0:
            return -10
        elif r_24h >= 2.0:
            return -5
    return 0


def _score_volume(volume_bucket: str) -> int:
    """Оценивает объём торгов."""
    if volume_bucket == "ultra":
        return 10
    elif volume_bucket == "high":
        return 7
    elif volume_bucket == "medium":
        return 5
    elif volume_bucket == "low":
        return 2
    elif volume_bucket == "trash":
        return -10
    return 0


def _score_sr_signal(sr_signal: str, is_long: bool) -> int:
    """Оценивает S/R сигнал."""
    if is_long:
        if sr_signal == "bounce_support":
            return 15
        elif sr_signal == "breakout_up":
            return 12
        elif sr_signal == "retest_broken_resistance_long":
            return 10
        elif sr_signal == "breakout_down":
            return -10
        elif sr_signal == "bounce_resistance":
            return -5
    else:
        if sr_signal == "bounce_resistance":
            return 15
        elif sr_signal == "breakout_down":
            return 12
        elif sr_signal == "retest_broken_support_short":
            return 10
        elif sr_signal == "breakout_up":
            return -10
        elif sr_signal == "bounce_support":
            return -5
    return 0


def _score_relative_strength(rs: Optional[float], is_long: bool) -> int:
    """Оценивает относительную силу к BTC."""
    if rs is None:
        return 0
    
    if is_long:
        if rs >= 2.0:
            return 10
        elif rs >= 1.0:
            return 7
        elif rs >= 0.5:
            return 3
        elif rs <= -1.0:
            return -5
        elif rs <= -0.5:
            return -3
    else:
        if rs <= -2.0:
            return 10
        elif rs <= -1.0:
            return 7
        elif rs <= -0.5:
            return 3
        elif rs >= 1.0:
            return -5
        elif rs >= 0.5:
            return -3
    return 0


def _score_market_mode(market_mode: str, is_long: bool) -> int:
    """Оценивает режим рынка."""
    if is_long:
        if market_mode in ("bull", "bull_sideways"):
            return 10
        elif market_mode == "sideways":
            return 5
        elif market_mode in ("bear", "bear_sideways"):
            return -5
    else:
        if market_mode in ("bear", "bear_sideways"):
            return 10
        elif market_mode == "sideways":
            return 5
        elif market_mode in ("bull", "bull_sideways"):
            return -5
    return 0


async def calculate_score(features: dict, direction: str, market_mode: str, ml_forecast: dict = None) -> int:
    """
    Рассчитывает score для входа.
    direction: 'long' или 'short'
    ml_forecast: прогноз от ML (с ключами direction, direction_probability)
    Возвращает число от 0 до 100
    """
    is_long = direction == "long"
    weights_data = await get_weights()
    weights = weights_data["weights"]
    
    # Получаем значения из features
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
    
    # Считаем очки по каждому фактору
    scores = {
        "distance": _score_distance(dist, is_long),
        "rsi": _score_rsi(rsi, is_long),
        "momentum_1h": _score_momentum_1h(r_1h, is_long),
        "momentum_24h": _score_momentum_24h(r_24h, is_long),
        "volume": _score_volume(volume_bucket),
        "sr_signal": _score_sr_signal(sr_signal, is_long),
        "relative_strength": _score_relative_strength(rs, is_long),
        "market_mode": _score_market_mode(market_mode, is_long),
    }
    
    # ML фактор
    ml_score = 0
    if ml_forecast:
        ml_dir = ml_forecast.get("direction")
        ml_prob = float(ml_forecast.get("direction_probability") or 0)
        if direction == "long" and ml_dir == "up":
            ml_score = int(ml_prob * 0.2)  # до 20 баллов
        elif direction == "short" and ml_dir == "down":
            ml_score = int(ml_prob * 0.2)
        elif ml_dir and ml_dir != direction:
            ml_score = -15  # штраф за противоречие
    scores["ml_signal"] = ml_score
    
    # Применяем веса
    total_score = 0
    for factor, score in scores.items():
        weight = weights.get(factor, 0.05)
        total_score += score * weight
    
    # Нормализуем до 0-100
    final_score = min(100, max(0, int(total_score)))
    
    logger.debug(f"Score for {direction}: total={final_score} | factors={scores} | weights={weights}")
    
    return final_score


async def should_enter_long(features: dict, forecast: dict, market_mode: str) -> Tuple[bool, int, str]:
    """Принимает решение о входе в long позицию."""
    direction = forecast.get("direction")
    prob = float(forecast.get("direction_probability") or 0)
    
    score = await calculate_score(features, "long", market_mode, ml_forecast=forecast)
    threshold = await get_entry_threshold()
    
    rsi = float(features.get("rsi_14")) if features.get("rsi_14") is not None else None
    dist = float(features.get("distance_to_support_pct")) if features.get("distance_to_support_pct") is not None else None
    volume_bucket = features.get("volume_bucket") or "low"
    
    # Экстремальные условия
    if rsi is not None and rsi <= 20 and dist is not None and dist <= 0.5:
        if volume_bucket not in ("trash", "low"):
            return True, score, f"extreme_oversold_rsi={rsi:.1f}_dist={dist:.2f}"
    
    # Основное правило
    if direction in ("up", "neutral"):
        if prob >= 55 and score >= threshold:
            return True, score, f"score={score}_prob={prob:.0f}"
        elif score >= threshold + 15:
            return True, score, f"high_score_only={score}"
    
    return False, score, f"score={score}<{threshold}_or_prob={prob:.0f}"


async def should_enter_short(features: dict, forecast: dict, market_mode: str) -> Tuple[bool, int, str]:
    """Принимает решение о входе в short позицию."""
    direction = forecast.get("direction")
    prob = float(forecast.get("direction_probability") or 0)
    
    score = await calculate_score(features, "short", market_mode, ml_forecast=forecast)
    threshold = await get_entry_threshold()
    
    rsi = float(features.get("rsi_14")) if features.get("rsi_14") is not None else None
    dist = float(features.get("distance_to_resistance_pct")) if features.get("distance_to_resistance_pct") is not None else None
    volume_bucket = features.get("volume_bucket") or "low"
    
    # Экстремальные условия
    if rsi is not None and rsi >= 80 and dist is not None and dist <= 0.5:
        if volume_bucket not in ("trash", "low"):
            return True, score, f"extreme_overbought_rsi={rsi:.1f}_dist={dist:.2f}"
    
    # Основное правило
    if direction in ("down", "neutral"):
        if prob >= 55 and score >= threshold:
            return True, score, f"score={score}_prob={prob:.0f}"
        elif score >= threshold + 15:
            return True, score, f"high_score_only={score}"
    
    return False, score, f"score={score}<{threshold}_or_prob={prob:.0f}"


async def should_enter(features: dict, forecast: dict, market_mode: str, direction: str) -> Tuple[bool, int, str]:
    """Универсальная функция для принятия решения."""
    if direction == "long":
        return await should_enter_long(features, forecast, market_mode)
    elif direction == "short":
        return await should_enter_short(features, forecast, market_mode)
    else:
        return False, 0, f"invalid_direction={direction}"

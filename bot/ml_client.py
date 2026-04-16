"""
ML Client — асинхронный клиент для ML-агента
"""

import aiohttp
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("ml_client")

ML_AGENT_URL = "http://ml-agent.railway.internal:8000"

async def get_ml_prediction(features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Отправляет признаки в ML-агент и получает прогноз.
    Возвращает: {"direction": "up/down/neutral", "direction_probability": float, "confidence": float}
    """
    try:
        # Все признаки включая новые (candle, FVG, OB, MS)
        FEATURE_NAMES = [
            'rsi_14', 'macd', 'macd_signal', 'macd_histogram',
            'bollinger_width', 'atr', 'r_1h', 'r_24h',
            'volume_24h', 'impulse_score', 'reversal_score',
            'relative_strength',
            'distance_to_support_pct', 'distance_to_resistance_pct',
            'candle_score_long', 'candle_score_short',
            'in_bullish_fvg', 'in_bearish_fvg',
            'in_bullish_ob', 'in_bearish_ob',
        ]
        clean = {}
        for k in FEATURE_NAMES:
            v = features.get(k)
            try:
                clean[k] = float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                clean[k] = 0.0

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ML_AGENT_URL}/predict",
                json={"features": clean},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    logger.debug(f"ML prediction: {result}")
                    return result
                else:
                    logger.warning(f"ML agent returned {resp.status}")
                    return None
    except Exception as e:
        logger.warning(f"ML agent error (url={ML_AGENT_URL}): {e}")
        return None

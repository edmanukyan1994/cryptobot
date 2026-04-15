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
        # Конвертируем все значения в JSON-совместимые типы
        clean = {}
        for k, v in features.items():
            if isinstance(v, (int, float, str, bool)) or v is None:
                clean[k] = v
            else:
                try:
                    clean[k] = float(v)
                except (TypeError, ValueError):
                    clean[k] = str(v)

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

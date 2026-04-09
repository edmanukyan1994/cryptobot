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
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ML_AGENT_URL}/predict",
                json={"features": features},
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
        logger.debug(f"ML agent error: {e}")
        return None

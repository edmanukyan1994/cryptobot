"""
ML Client — асинхронный клиент для ML-агента
"""

import aiohttp
import asyncio
import logging
import os
from typing import Optional, Dict, Any

logger = logging.getLogger("ml_client")

_DEFAULT_INTERNAL_URL = "http://ml-agent.railway.internal:8000"
_DEFAULT_PUBLIC_URL = "https://ml-agent-production-591a.up.railway.app"

# Можно переопределить:
# - ML_AGENT_URLS="http://..,https://.."
# - или один ML_AGENT_URL
_urls_env = os.getenv("ML_AGENT_URLS", "").strip()
if _urls_env:
    ML_AGENT_URLS = [u.strip().rstrip("/") for u in _urls_env.split(",") if u.strip()]
else:
    single = os.getenv("ML_AGENT_URL", _DEFAULT_INTERNAL_URL).strip().rstrip("/")
    ML_AGENT_URLS = [single]
    if single != _DEFAULT_PUBLIC_URL:
        ML_AGENT_URLS.append(_DEFAULT_PUBLIC_URL)

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


async def _predict_with_url(url: str, payload: dict) -> Optional[Dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=5)
    ssl_modes = [None]
    if url.startswith("https://"):
        # Fallback only for environments with broken root CA bundle.
        ssl_modes.append(False)

    for ssl_mode in ssl_modes:
        for attempt in (1, 2):  # 1 retry на URL
            try:
                if ssl_mode is None:
                    session = aiohttp.ClientSession(timeout=timeout)
                else:
                    connector = aiohttp.TCPConnector(ssl=ssl_mode)
                    session = aiohttp.ClientSession(timeout=timeout, connector=connector)

                async with session:
                    async with session.post(f"{url}/predict", json=payload) as resp:
                        if resp.status != 200:
                            logger.warning(f"ML agent returned {resp.status} (url={url})")
                            return None
                        result = await resp.json()
                        if not isinstance(result, dict):
                            return None
                        return result
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == 1:
                    continue
                # Если стандартная TLS проверка упала, попробуем ssl=False режим.
                if ssl_mode is None and url.startswith("https://"):
                    break
                logger.debug(f"ML transport error (url={url}): {e}")
            except Exception as e:
                logger.warning(f"ML unexpected error (url={url}): {e}")
                return None
    return None

async def get_ml_prediction(features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Отправляет признаки в ML-агент и получает прогноз.
    Возвращает: {"direction": "up/down/neutral", "direction_probability": float, "confidence": float}
    """
    clean: Dict[str, float] = {}
    for k in FEATURE_NAMES:
        v = features.get(k)
        try:
            clean[k] = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            clean[k] = 0.0

    payload = {"features": clean}
    for url in ML_AGENT_URLS:
        result = await _predict_with_url(url, payload)
        if result:
            logger.debug(f"ML prediction (url={url}): {result}")
            return result

    logger.warning(f"ML prediction failed on all URLs: {ML_AGENT_URLS}")
    return None

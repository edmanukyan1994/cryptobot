"""
Market Context Module — глобальный контекст рынка.

Собирает каждые 30 минут:
1. BTC тренд (MA20/MA50/MA200 на 1D и 4H)
2. BTC объём (растущий/падающий)
3. BTC доминирование
4. Общий режим рынка
5. Структуру и моментум BTC

Используется forecaster'ом и trader'ом как глобальный фильтр.
"""
import asyncio
import aiohttp
import logging
import numpy as np
from datetime import datetime, timezone
import db

logger = logging.getLogger("market_context")
BYBIT_BASE = "https://api.bybit.com"

# Глобальный кэш контекста (обновляется каждые 30 мин)
_context: dict = {}


def get_context() -> dict:
    return dict(_context)


async def fetch_klines(session, symbol: str, interval: str, limit: int) -> list:
    try:
        async with session.get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json()
            if data.get("retCode") != 0:
                return []
            return [
                {
                    "open": float(x[1]),
                    "high": float(x[2]),
                    "low": float(x[3]),
                    "close": float(x[4]),
                    "volume": float(x[5]),
                }
                for x in reversed(data["result"]["list"])
            ]
    except Exception as e:
        logger.debug(f"fetch_klines {symbol} {interval}: {e}")
        return []


def calc_ma(prices: list, period: int) -> float | None:
    if len(prices) < period:
        return None
    return float(np.mean(prices[-period:]))


def calc_volume_trend(volumes: list, period: int = 5) -> str:
    """Определяет тренд объёма — растёт или падает."""
    if len(volumes) < period * 2:
        return "neutral"

    recent = np.mean(volumes[-period:])
    prev = np.mean(volumes[-period * 2:-period])

    if recent > prev * 1.2:
        return "increasing"
    elif recent < prev * 0.8:
        return "decreasing"
    return "neutral"


def calc_price_structure(candles: list) -> str:
    """
    Определяет структуру цены.
    Higher highs + higher lows = uptrend
    Lower highs + lower lows = downtrend
    """
    if len(candles) < 10:
        return "sideways"

    highs = [c["high"] for c in candles[-10:]]
    lows = [c["low"] for c in candles[-10:]]

    hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i - 1])
    lh = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1])
    hl = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
    ll = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i - 1])

    if hh > lh and hl > ll:
        return "uptrend"
    elif lh > hh and ll > hl:
        return "downtrend"
    return "sideways"


def detect_momentum(btc_24h_change: float, btc_7d_change: float) -> str:
    """
    Краткосрочный моментум BTC.
    """
    if btc_24h_change <= -3.0:
        return "strong_down"
    if btc_24h_change <= -1.0:
        return "weak_down"
    if btc_24h_change >= 3.0:
        return "strong_up"
    if btc_24h_change >= 1.0:
        return "weak_up"

    # добивка 7d, если 24h плоский
    if btc_7d_change <= -6.0:
        return "weak_down"
    if btc_7d_change >= 6.0:
        return "weak_up"

    return "flat"


def detect_market_mode(global_regime: str, price_structure_4h: str) -> str:
    """
    Режим для trader:
    - bear
    - bear_sideways
    - bull
    - bull_sideways
    - sideways
    """
    if global_regime in ("bear_market", "mild_bear"):
        if price_structure_4h == "downtrend":
            return "bear"
        if price_structure_4h == "sideways":
            return "bear_sideways"

    if global_regime in ("bull_market", "mild_bull"):
        if price_structure_4h == "uptrend":
            return "bull"
        if price_structure_4h == "sideways":
            return "bull_sideways"

    return "sideways"


def detect_crash_mode(btc_24h_change: float, btc_7d_change: float, above_ma200_1d: bool | None) -> bool:
    """
    Отдельный crash-флаг.
    Он полезен trader'у и forecaster'у независимо от общего режима.
    """
    if btc_24h_change <= -4.5:
        return True
    if btc_24h_change <= -3.0 and not above_ma200_1d:
        return True
    if btc_7d_change <= -10.0 and not above_ma200_1d:
        return True
    return False


async def update_market_context():
    """Обновляет глобальный контекст рынка."""
    global _context

    try:
        async with aiohttp.ClientSession() as session:
            # BTC данные на разных таймфреймах
            btc_1d = await fetch_klines(session, "BTCUSDT", "D", 200)
            btc_4h = await fetch_klines(session, "BTCUSDT", "240", 100)
            btc_1h = await fetch_klines(session, "BTCUSDT", "60", 48)

            if not btc_1d or not btc_4h:
                logger.warning("Failed to fetch BTC klines for market context")
                return

            btc_price = btc_1d[-1]["close"]
            btc_closes_1d = [c["close"] for c in btc_1d]
            btc_closes_4h = [c["close"] for c in btc_4h]
            btc_volumes_1d = [c["volume"] for c in btc_1d]

            # MA на дневном таймфрейме
            ma20_1d = calc_ma(btc_closes_1d, 20)
            ma50_1d = calc_ma(btc_closes_1d, 50)
            ma200_1d = calc_ma(btc_closes_1d, 200)

            # MA на 4H таймфрейме
            ma20_4h = calc_ma(btc_closes_4h, 20)
            ma50_4h = calc_ma(btc_closes_4h, 50)

            # Тренд по MA
            above_ma20_1d = btc_price > ma20_1d if ma20_1d else None
            above_ma50_1d = btc_price > ma50_1d if ma50_1d else None
            above_ma200_1d = btc_price > ma200_1d if ma200_1d else None
            above_ma20_4h = btc_price > ma20_4h if ma20_4h else None
            above_ma50_4h = btc_price > ma50_4h if ma50_4h else None

            # Структура цены
            price_structure_1d = calc_price_structure(btc_1d)
            price_structure_4h = calc_price_structure(btc_4h)

            # Объём тренд
            vol_trend_1d = calc_volume_trend(btc_volumes_1d)

            # 24h изменение BTC
            btc_24h_change = (
                (btc_price - btc_1d[-2]["close"]) / btc_1d[-2]["close"] * 100
                if len(btc_1d) >= 2 else 0
            )

            # 7d изменение BTC
            btc_7d_change = (
                (btc_price - btc_1d[-8]["close"]) / btc_1d[-8]["close"] * 100
                if len(btc_1d) >= 8 else 0
            )

            # 4h short-term move
            btc_4h_change = (
                (btc_closes_4h[-1] - btc_closes_4h[-2]) / btc_closes_4h[-2] * 100
                if len(btc_closes_4h) >= 2 else 0
            )

            # Определяем глобальный режим рынка
            bull_signals = 0
            bear_signals = 0

            if above_ma200_1d:
                bull_signals += 3
            else:
                bear_signals += 3

            if above_ma50_1d:
                bull_signals += 2
            else:
                bear_signals += 2

            if above_ma20_1d:
                bull_signals += 1
            else:
                bear_signals += 1

            if price_structure_1d == "uptrend":
                bull_signals += 2
            elif price_structure_1d == "downtrend":
                bear_signals += 2

            if price_structure_4h == "uptrend":
                bull_signals += 1
            elif price_structure_4h == "downtrend":
                bear_signals += 1

            if btc_24h_change > 2:
                bull_signals += 1
            elif btc_24h_change < -2:
                bear_signals += 1

            if btc_7d_change > 5:
                bull_signals += 1
            elif btc_7d_change < -5:
                bear_signals += 1

            total = bull_signals + bear_signals
            bull_pct = bull_signals / total * 100 if total > 0 else 50
            bear_pct = bear_signals / total * 100 if total > 0 else 50

            if detect_crash_mode(btc_24h_change, btc_7d_change, above_ma200_1d):
                global_regime = "crash"
            elif bull_pct >= 70:
                global_regime = "bull_market"
            elif bull_pct >= 55:
                global_regime = "mild_bull"
            elif bear_pct >= 70:
                global_regime = "bear_market"
            elif bear_pct >= 55:
                global_regime = "mild_bear"
            else:
                global_regime = "neutral"

            # Сила тренда (0-100)
            trend_strength = abs(bull_signals - bear_signals) / total * 100 if total > 0 else 0

            # Новый моментум
            momentum = detect_momentum(btc_24h_change, btc_7d_change)

            # Новый market_mode
            market_mode = detect_market_mode(global_regime, price_structure_4h)

            # regime_score: отрицательный = bearish, положительный = bullish
            regime_score = round(bull_pct - bear_pct, 1)

            # --- DERIVED SIGNALS ДЛЯ TRADER / FORECASTER ---
            is_aggressive_bear = (
                global_regime in ("bear_market", "crash")
                and momentum in ("strong_down", "weak_down")
            )

            is_aggressive_bull = (
                global_regime in ("bull_market", "mild_bull")
                and momentum in ("strong_up", "weak_up")
            )

            no_long_zone = global_regime == "crash" or momentum == "strong_down"
            no_short_zone = global_regime == "bull_market" and momentum == "strong_up"

            btc_move_strength = abs(btc_24h_change) + abs(btc_4h_change)

            # BTC доминирование из БД
            btc_dom_row = await db.fetchrow(
                "SELECT btc_dominance FROM crypto_market_global WHERE id='latest'"
            )
            btc_dominance = float(btc_dom_row["btc_dominance"]) if btc_dom_row else 55.0

            # Fear & Greed
            fg_row = await db.fetchrow("SELECT value FROM crypto_fear_greed WHERE id='latest'")
            fg = float(fg_row["value"]) if fg_row else 50.0

            _context = {
                # базовые данные
                "btc_price": btc_price,
                "btc_24h_change": round(btc_24h_change, 2),
                "btc_7d_change": round(btc_7d_change, 2),
                "btc_4h_change": round(btc_4h_change, 2),

                # MA
                "btc_ma20_1d": round(ma20_1d, 2) if ma20_1d else None,
                "btc_ma50_1d": round(ma50_1d, 2) if ma50_1d else None,
                "btc_ma200_1d": round(ma200_1d, 2) if ma200_1d else None,
                "btc_ma20_4h": round(ma20_4h, 2) if ma20_4h else None,
                "btc_ma50_4h": round(ma50_4h, 2) if ma50_4h else None,

                # положения цены
                "above_ma20_1d": above_ma20_1d,
                "above_ma50_1d": above_ma50_1d,
                "above_ma200_1d": above_ma200_1d,
                "above_ma20_4h": above_ma20_4h,
                "above_ma50_4h": above_ma50_4h,

                # структура
                "price_structure_1d": price_structure_1d,
                "price_structure_4h": price_structure_4h,
                "vol_trend_1d": vol_trend_1d,

                # старые поля
                "global_regime": global_regime,
                "trend_strength": round(trend_strength, 1),
                "bull_signals": bull_signals,
                "bear_signals": bear_signals,

                # новые поля
                "market_mode": market_mode,
                "momentum": momentum,
                "regime_score": regime_score,
                "is_crash": global_regime == "crash",
                "is_aggressive_bear": is_aggressive_bear,
                "is_aggressive_bull": is_aggressive_bull,
                "no_long_zone": no_long_zone,
                "no_short_zone": no_short_zone,
                "btc_move_strength": round(btc_move_strength, 2),

                # внешние данные
                "btc_dominance": btc_dominance,
                "fear_greed": fg,

                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            logger.info(
                f"Market context: {global_regime} | mode={market_mode} | "
                f"BTC={btc_price:,.0f} "
                f"({'>' if above_ma200_1d else '<'}MA200) "
                f"24h={btc_24h_change:+.1f}% "
                f"7d={btc_7d_change:+.1f}% "
                f"struct4h={price_structure_4h} "
                f"momentum={momentum} "
                f"vol={vol_trend_1d}"
            )

    except Exception as e:
        logger.error(f"Market context update error: {e}")


async def run_market_context():
    """Запускает обновление контекста каждые 30 минут."""
    logger.info("Market context module started")
    while True:
        await update_market_context()
        await asyncio.sleep(1800)  # 30 минут


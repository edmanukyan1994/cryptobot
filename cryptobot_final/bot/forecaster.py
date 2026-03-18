import asyncio
import math
from datetime import datetime, timezone
from loguru import logger
import db

# ============================================================
# БАЗОВЫЕ ВЕСА ФАКТОРОВ (если в БД нет данных)
# ============================================================
DEFAULT_WEIGHTS = {
    "momentum":    0.27,
    "rsi":         0.16,
    "sr":          0.12,
    "bollinger":   0.11,
    "macd":        0.11,
    "candlestick": 0.10,
    "fear_greed":  0.09,
    "regime":      0.04,
}

# Горизонты и их длительность в часах
HORIZONS = {"1h": 1, "4h": 4, "24h": 24}

# ============================================================
# ЗАГРУЗКА ВЕСОВ ИЗ БД
# ============================================================

async def get_factor_weights(symbol: str, horizon: str) -> dict[str, float]:
    """Загружает выученные веса факторов для символа/горизонта."""
    rows = await db.fetch(
        """SELECT factor_name, current_weight FROM crypto_factor_weights
           WHERE symbol=$1 AND horizon=$2""",
        symbol, horizon
    )
    if not rows:
        return {k: 1.0 for k in DEFAULT_WEIGHTS}

    weights = {r["factor_name"]: float(r["current_weight"]) for r in rows}
    # Заполняем дефолтами если чего-то нет
    for k in DEFAULT_WEIGHTS:
        if k not in weights:
            weights[k] = 1.0
    return weights

# ============================================================
# 8 ФАКТОРОВ
# ============================================================

def score_momentum(r_1h: float, r_24h: float, regime: str) -> tuple[float, float]:
    """Фактор Momentum. Возвращает (bullish_score, bearish_score)."""
    bull, bear = 0.0, 0.0

    # В крэше RSI<25 = медвежий сигнал (инвертированный)
    if regime == "crash":
        if r_1h < 0:
            bear += 0.6
        if r_24h < -5:
            bear += 0.8
        return bull, bear

    # Нормальный режим
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

    # Выравнивание: если оба указывают в одну сторону — усиливаем
    if bull > 0 and r_1h > 0:
        bull += 0.2
    if bear > 0 and r_1h < 0:
        bear += 0.2

    return min(bull, 1.5), min(bear, 1.5)

def score_rsi(rsi: float | None, regime: str) -> tuple[float, float]:
    """Фактор RSI."""
    if rsi is None:
        return 0.0, 0.0
    bull, bear = 0.0, 0.0

    if regime == "crash":
        # В крэше перепроданность = продолжение падения
        if rsi < 25:
            bear += 0.9
        elif rsi < 35:
            bear += 0.5
        return bull, bear

    if rsi < 25:
        bull += 1.0  # сильно перепродан
    elif rsi < 35:
        bull += 0.6
    elif rsi < 45:
        bull += 0.2
    elif rsi > 75:
        bear += 1.0  # сильно перекуплен
    elif rsi > 65:
        bear += 0.6
    elif rsi > 55:
        bear += 0.2

    return bull, bear

def score_bollinger(price: float, bb_upper: float | None, bb_lower: float | None,
                    bb_middle: float | None) -> tuple[float, float]:
    """Фактор Bollinger Bands. Позиция цены в канале 0-100%."""
    if not all([bb_upper, bb_lower, bb_middle]) or bb_upper == bb_lower:
        return 0.0, 0.0
    bull, bear = 0.0, 0.0
    pos = (price - bb_lower) / (bb_upper - bb_lower)  # 0 = нижняя, 1 = верхняя

    if pos <= 0.1:
        bull += 1.0
    elif pos <= 0.25:
        bull += 0.5
    elif pos >= 0.9:
        bear += 1.0
    elif pos >= 0.75:
        bear += 0.5

    return bull, bear

def score_macd(macd: float | None, macd_signal: float | None,
               macd_hist: float | None) -> tuple[float, float]:
    """Фактор MACD."""
    bull, bear = 0.0, 0.0
    if macd_hist is not None:
        if macd_hist > 0.001:
            bull += min(0.8, macd_hist * 100)
        elif macd_hist < -0.001:
            bear += min(0.8, abs(macd_hist) * 100)
    if macd is not None and macd_signal is not None:
        if macd > macd_signal:
            bull += 0.3
        elif macd < macd_signal:
            bear += 0.3
    return min(bull, 1.2), min(bear, 1.2)

def score_candlestick(pattern: str, score: float) -> tuple[float, float]:
    """Фактор свечных паттернов."""
    if score > 0:
        return score, 0.0
    elif score < 0:
        return 0.0, abs(score)
    return 0.0, 0.0

def score_fear_greed(fg: float | None) -> tuple[float, float]:
    """Фактор Fear & Greed."""
    if fg is None:
        return 0.0, 0.0
    bull, bear = 0.0, 0.0
    if fg <= 15:
        bull += 0.8  # Экстремальный страх = возможность для покупки
        bear += 0.4  # Но также = риск
    elif fg <= 25:
        bull += 0.4
        bear += 0.3
    elif fg >= 80:
        bear += 0.8
    elif fg >= 65:
        bear += 0.4

    return bull, bear

def score_regime(regime: str) -> tuple[float, float]:
    """Фактор режима рынка."""
    mapping = {
        "bullish": (0.8, 0.0),
        "bearish": (0.0, 0.8),
        "crash": (0.0, 1.0),
        "oversold_crash": (0.2, 0.6),
        "euphoria": (0.0, 0.7),
        "consolidation": (0.1, 0.1),
        "neutral": (0.0, 0.0),
    }
    return mapping.get(regime, (0.0, 0.0))

def score_sr(price: float, support: float | None, resistance: float | None,
             sr_signal: str) -> tuple[float, float]:
    """Фактор Support/Resistance."""
    bull, bear = 0.0, 0.0
    if sr_signal == "bounce_support":
        bull += 0.7
    elif sr_signal == "bounce_resistance":
        bear += 0.7
    elif sr_signal == "breakout_up":
        bull += 0.9
    elif sr_signal == "breakout_down":
        bear += 0.9

    if support and price > 0:
        dist = (price - support) / price * 100
        if dist < 1.0:
            bull += 0.4
    if resistance and price > 0:
        dist = (resistance - price) / price * 100
        if dist < 1.0:
            bear += 0.4

    return min(bull, 1.2), min(bear, 1.2)

# ============================================================
# РАСЧЁТ ПРОГНОЗА
# ============================================================

def calc_probability(bull_total: float, bear_total: float) -> tuple[str, float, float]:
    """
    Вычисляет direction, probability, confidence.
    Точная копия логики из run-crypto-forecasts.
    """
    score_diff = bull_total - bear_total
    total = bull_total + bear_total

    if total < 0.1:
        return "neutral", 50.0, 40.0

    if abs(score_diff) < 0.15:
        direction = "neutral"
        probability = 50.0
    elif score_diff > 0:
        direction = "up"
        probability = min(80, 50 + score_diff * 15)
    else:
        direction = "down"
        probability = min(80, 50 + abs(score_diff) * 15)

    # Confidence = насколько сигналы согласованы
    if total > 0:
        dominant = max(bull_total, bear_total)
        confidence = min(85, (dominant / total) * 100)
    else:
        confidence = 40.0

    return direction, round(probability, 1), round(confidence, 1)

def calc_price_corridor(price: float, atr: float | None, horizon_hours: int,
                        regime: str) -> tuple[float, float, float]:
    """
    Строит P10/P50/P90 коридор через ATR.
    Адаптивный: bearish режим расширяет нижнюю границу.
    """
    if not atr or price <= 0:
        # Фоллбэк: фиксированные %
        default_pcts = {1: 0.015, 4: 0.035, 24: 0.07}
        pct = default_pcts.get(horizon_hours, 0.05)
        return round(price * (1 - pct), 6), round(price, 6), round(price * (1 + pct), 6)

    # Адаптивный ATR: sqrt(horizon) масштабирование
    atr_pct = atr / price
    half_band = atr_pct * math.sqrt(horizon_hours) * 0.25

    # Режимная асимметрия
    if regime in ("bearish", "crash"):
        lower_mult = 1.4
        upper_mult = 0.8
    elif regime == "bullish":
        lower_mult = 0.8
        upper_mult = 1.2
    else:
        lower_mult = 1.0
        upper_mult = 1.0

    p50 = price
    p10 = round(price * (1 - half_band * lower_mult), 6)
    p90 = round(price * (1 + half_band * upper_mult), 6)

    return p10, p50, p90

async def run_forecast_for_symbol(symbol: str, features: dict) -> list[dict]:
    """Запускает прогноз для символа по всем горизонтам."""
    results = []

    for horizon, hours in HORIZONS.items():
        try:
            weights = await get_factor_weights(symbol, horizon)

            # Считаем каждый фактор
            factor_scores = {}

            m_bull, m_bear = score_momentum(features["r_1h"], features["r_24h"], features["regime"])
            factor_scores["momentum"] = (m_bull * weights.get("momentum", 1.0),
                                          m_bear * weights.get("momentum", 1.0))

            r_bull, r_bear = score_rsi(features["rsi_14"], features["regime"])
            factor_scores["rsi"] = (r_bull * weights.get("rsi", 1.0),
                                     r_bear * weights.get("rsi", 1.0))

            b_bull, b_bear = score_bollinger(
                features["price"], features["bollinger_upper"],
                features["bollinger_lower"], features["bollinger_middle"]
            )
            factor_scores["bollinger"] = (b_bull * weights.get("bollinger", 1.0),
                                           b_bear * weights.get("bollinger", 1.0))

            ma_bull, ma_bear = score_macd(features["macd"], features["macd_signal"], features["macd_histogram"])
            factor_scores["macd"] = (ma_bull * weights.get("macd", 1.0),
                                      ma_bear * weights.get("macd", 1.0))

            c_bull, c_bear = score_candlestick(features["candlestick_pattern"], features["candlestick_score"])
            factor_scores["candlestick"] = (c_bull * weights.get("candlestick", 1.0),
                                             c_bear * weights.get("candlestick", 1.0))

            fg_bull, fg_bear = score_fear_greed(features["fear_greed_index"])
            factor_scores["fear_greed"] = (fg_bull * weights.get("fear_greed", 1.0),
                                            fg_bear * weights.get("fear_greed", 1.0))

            reg_bull, reg_bear = score_regime(features["regime"])
            factor_scores["regime"] = (reg_bull * weights.get("regime", 1.0),
                                        reg_bear * weights.get("regime", 1.0))

            sr_bull, sr_bear = score_sr(
                features["price"], features["support_1"], features["resistance_1"],
                features["sr_signal"] or "neutral"
            )
            factor_scores["sr"] = (sr_bull * weights.get("sr", 1.0),
                                    sr_bear * weights.get("sr", 1.0))

            # Нормализованные веса DEFAULT_WEIGHTS как базис
            bull_total = sum(v[0] * DEFAULT_WEIGHTS.get(k, 0.1) for k, v in factor_scores.items())
            bear_total = sum(v[1] * DEFAULT_WEIGHTS.get(k, 0.1) for k, v in factor_scores.items())

            direction, probability, confidence = calc_probability(bull_total, bear_total)

            p10, p50, p90 = calc_price_corridor(
                features["price"], features["atr"], hours, features["regime"]
            )

            forecast = {
                "symbol": symbol,
                "horizon": horizon,
                "direction": direction,
                "direction_probability": probability,
                "confidence": confidence,
                "risk_score": features["risk_score"],
                "p10": p10,
                "p50": p50,
                "p90": p90,
                "regime": features["regime"],
                "features_snapshot": {
                    "rsi": features["rsi_14"],
                    "macd_hist": features["macd_histogram"],
                    "fear_greed": features["fear_greed_index"],
                    "r_24h": features["r_24h"],
                    "sr_signal": features["sr_signal"],
                    "bull_total": round(bull_total, 3),
                    "bear_total": round(bear_total, 3),
                },
                "created_at": datetime.now(timezone.utc),
            }
            results.append(forecast)

        except Exception as e:
            logger.warning(f"Forecast error {symbol}/{horizon}: {e}")

    return results

async def save_forecasts(forecasts: list[dict]) -> list[str]:
    """Сохраняет прогнозы в БД, возвращает список ID."""
    ids = []
    for fc in forecasts:
        row = await db.fetchrow(
            """INSERT INTO crypto_forecast_runs
               (symbol, horizon, direction, direction_probability, confidence,
                risk_score, p10, p50, p90, regime, features_snapshot, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               RETURNING id""",
            fc["symbol"], fc["horizon"], fc["direction"],
            fc["direction_probability"], fc["confidence"],
            fc["risk_score"], fc["p10"], fc["p50"], fc["p90"],
            fc["regime"], str(fc["features_snapshot"]), fc["created_at"]
        )
        if row:
            ids.append(str(row["id"]))
    return ids

async def get_latest_forecast(symbol: str, horizon: str = "4h") -> dict | None:
    """Возвращает последний прогноз для символа."""
    row = await db.fetchrow(
        """SELECT * FROM crypto_forecast_runs
           WHERE symbol=$1 AND horizon=$2
           ORDER BY created_at DESC LIMIT 1""",
        symbol, horizon
    )
    return dict(row) if row else None

async def run_forecaster():
    """Запускает прогнозы для всех символов каждые 10 минут."""
    logger.info("Forecaster started")

    while True:
        try:
            symbols = await db.fetch(
                "SELECT symbol FROM crypto_assets WHERE is_active=true ORDER BY rank"
            )

            forecasted = 0
            for row in symbols:
                symbol = row["symbol"]
                try:
                    # Берём последние фичи
                    features_row = await db.fetchrow(
                        """SELECT * FROM crypto_features_hourly
                           WHERE symbol=$1 ORDER BY ts DESC LIMIT 1""",
                        symbol
                    )
                    if not features_row:
                        continue

                    features = dict(features_row)
                    forecasts = await run_forecast_for_symbol(symbol, features)
                    if forecasts:
                        await save_forecasts(forecasts)
                        forecasted += 1

                except Exception as e:
                    logger.warning(f"Forecast error {symbol}: {e}")
                await asyncio.sleep(0.2)

            logger.info(f"Forecasts generated for {forecasted}/{len(symbols)} symbols")

        except Exception as e:
            logger.error(f"Forecaster loop error: {e}")

        await asyncio.sleep(600)  # каждые 10 минут

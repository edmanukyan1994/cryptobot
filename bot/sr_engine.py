"""
S/R Engine — три метода определения уровней поддержки/сопротивления.

Методы:
1. Локальные min/max — краткосрочные уровни
2. Pivot Points — классические дневные уровни (P, R1, R2, S1, S2)
3. Volume Profile — уровни по объёму (Point of Control)

Confluence score — чем больше методов подтверждают уровень, тем он надёжнее.
"""

import asyncio
import aiohttp
import logging
import numpy as np
from datetime import datetime, timezone
from config import bybit_symbol

logger = logging.getLogger("sr_engine")

BYBIT_BASE = "https://api.bybit.com"

# ============================================================
# ПОЛУЧЕНИЕ ДАННЫХ
# ============================================================

async def fetch_klines_full(session: aiohttp.ClientSession, symbol: str,
                             interval: str = "60", limit: int = 200) -> list:
    """Получает OHLCV свечи для анализа."""
    try:
        bs = bybit_symbol(symbol)
        async with session.get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "linear", "symbol": bs, "interval": interval, "limit": limit},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("retCode") != 0:
                return []
            candles = []
            for row in reversed(data["result"]["list"]):
                candles.append({
                    "ts": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            return candles
    except Exception as e:
        logger.warning(f"Klines fetch error {symbol}: {e}")
        return []

# ============================================================
# МЕТОД 1: ЛОКАЛЬНЫЕ MIN/MAX
# ============================================================

def find_local_extremes(candles: list, lookback: int = 5) -> dict:
    """
    Находит локальные минимумы и максимумы.
    Уровень сильнее если цена касалась его несколько раз.
    """
    if len(candles) < lookback * 2 + 1:
        return {"supports": [], "resistances": []}

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    supports = []
    resistances = []

    for i in range(lookback, len(candles) - lookback):
        # Локальный минимум
        if all(lows[i] <= lows[i-j] for j in range(1, lookback+1)) and \
           all(lows[i] <= lows[i+j] for j in range(1, lookback+1)):
            avg_vol = np.mean(volumes[max(0,i-lookback):i+lookback])
            touch_strength = volumes[i] / avg_vol if avg_vol > 0 else 1.0
            supports.append({
                "price": lows[i],
                "strength": min(1.0, touch_strength * 0.5),
                "method": "local_min",
                "index": i
            })

        # Локальный максимум
        if all(highs[i] >= highs[i-j] for j in range(1, lookback+1)) and \
           all(highs[i] >= highs[i+j] for j in range(1, lookback+1)):
            avg_vol = np.mean(volumes[max(0,i-lookback):i+lookback])
            touch_strength = volumes[i] / avg_vol if avg_vol > 0 else 1.0
            resistances.append({
                "price": highs[i],
                "strength": min(1.0, touch_strength * 0.5),
                "method": "local_max",
                "index": i
            })

    # Кластеризуем близкие уровни (в пределах 0.5%)
    supports = cluster_levels(supports, tolerance_pct=0.5)
    resistances = cluster_levels(resistances, tolerance_pct=0.5)

    return {"supports": supports, "resistances": resistances}

def cluster_levels(levels: list, tolerance_pct: float = 0.5) -> list:
    """Объединяет близкие уровни в кластеры."""
    if not levels:
        return []

    levels_sorted = sorted(levels, key=lambda x: x["price"])
    clusters = []
    current_cluster = [levels_sorted[0]]

    for level in levels_sorted[1:]:
        last_price = current_cluster[-1]["price"]
        if abs(level["price"] - last_price) / last_price * 100 <= tolerance_pct:
            current_cluster.append(level)
        else:
            # Финализируем кластер
            avg_price = np.mean([l["price"] for l in current_cluster])
            max_strength = max(l["strength"] for l in current_cluster)
            touch_count = len(current_cluster)
            clusters.append({
                "price": avg_price,
                "strength": min(1.0, max_strength + touch_count * 0.1),
                "touches": touch_count,
                "method": current_cluster[0]["method"]
            })
            current_cluster = [level]

    # Последний кластер
    if current_cluster:
        avg_price = np.mean([l["price"] for l in current_cluster])
        max_strength = max(l["strength"] for l in current_cluster)
        clusters.append({
            "price": avg_price,
            "strength": min(1.0, max_strength + len(current_cluster) * 0.1),
            "touches": len(current_cluster),
            "method": current_cluster[0]["method"]
        })

    return clusters

# ============================================================
# МЕТОД 2: PIVOT POINTS
# ============================================================

def calc_pivot_points(candles: list) -> dict:
    """
    Классические Pivot Points на основе последней дневной свечи.
    P = (H + L + C) / 3
    R1 = 2P - L, R2 = P + (H-L), R3 = H + 2(P-L)
    S1 = 2P - H, S2 = P - (H-L), S3 = L - 2(H-P)
    """
    if len(candles) < 24:
        return {"supports": [], "resistances": [], "pivot": None}

    # Берём последние 24 часа как "дневную свечу"
    last_day = candles[-24:]
    high = max(c["high"] for c in last_day)
    low = min(c["low"] for c in last_day)
    close = last_day[-1]["close"]

    p = (high + low + close) / 3

    r1 = 2 * p - low
    r2 = p + (high - low)
    r3 = high + 2 * (p - low)

    s1 = 2 * p - high
    s2 = p - (high - low)
    s3 = low - 2 * (high - p)

    resistances = [
        {"price": r1, "strength": 0.8, "touches": 1, "method": "pivot_r1"},
        {"price": r2, "strength": 0.6, "touches": 1, "method": "pivot_r2"},
        {"price": r3, "strength": 0.4, "touches": 1, "method": "pivot_r3"},
    ]
    supports = [
        {"price": s1, "strength": 0.8, "touches": 1, "method": "pivot_s1"},
        {"price": s2, "strength": 0.6, "touches": 1, "method": "pivot_s2"},
        {"price": s3, "strength": 0.4, "touches": 1, "method": "pivot_s3"},
    ]

    return {
        "supports": [s for s in supports if s["price"] > 0],
        "resistances": resistances,
        "pivot": p
    }

# ============================================================
# МЕТОД 3: VOLUME PROFILE
# ============================================================

def calc_volume_profile(candles: list, bins: int = 50) -> dict:
    """
    Volume Profile — находит Point of Control (POC) и Value Area.
    POC = цена с максимальным объёмом торгов.
    Value Area = 70% всего объёма вокруг POC.
    """
    if len(candles) < 20:
        return {"supports": [], "resistances": [], "poc": None}

    prices_all = []
    volumes_all = []

    for c in candles:
        # Распределяем объём равномерно между High и Low
        typical = (c["high"] + c["low"] + c["close"]) / 3
        prices_all.append(typical)
        volumes_all.append(c["volume"])

    if not prices_all:
        return {"supports": [], "resistances": [], "poc": None}

    min_price = min(c["low"] for c in candles)
    max_price = max(c["high"] for c in candles)

    if min_price >= max_price:
        return {"supports": [], "resistances": [], "poc": None}

    # Создаём ценовые бины
    bin_size = (max_price - min_price) / bins
    bin_volumes = [0.0] * bins
    bin_prices = [min_price + (i + 0.5) * bin_size for i in range(bins)]

    for price, volume in zip(prices_all, volumes_all):
        bin_idx = int((price - min_price) / bin_size)
        bin_idx = max(0, min(bins - 1, bin_idx))
        bin_volumes[bin_idx] += volume

    # Point of Control — бин с максимальным объёмом
    poc_idx = bin_volumes.index(max(bin_volumes))
    poc_price = bin_prices[poc_idx]

    # Value Area — 70% объёма вокруг POC
    total_vol = sum(bin_volumes)
    target_vol = total_vol * 0.70
    accumulated = bin_volumes[poc_idx]
    low_idx, high_idx = poc_idx, poc_idx

    while accumulated < target_vol:
        expand_low = low_idx > 0
        expand_high = high_idx < bins - 1
        if not expand_low and not expand_high:
            break
        if expand_low and expand_high:
            if bin_volumes[low_idx-1] >= bin_volumes[high_idx+1]:
                low_idx -= 1
                accumulated += bin_volumes[low_idx]
            else:
                high_idx += 1
                accumulated += bin_volumes[high_idx]
        elif expand_low:
            low_idx -= 1
            accumulated += bin_volumes[low_idx]
        else:
            high_idx += 1
            accumulated += bin_volumes[high_idx]

    val_low = bin_prices[low_idx]   # Value Area Low (VAL) — поддержка
    val_high = bin_prices[high_idx]  # Value Area High (VAH) — сопротивление

    current_price = candles[-1]["close"]

    supports = []
    resistances = []

    # POC как уровень
    poc_strength = 0.9  # POC — самый сильный уровень
    if poc_price < current_price:
        supports.append({"price": poc_price, "strength": poc_strength, "touches": 1, "method": "volume_poc"})
    else:
        resistances.append({"price": poc_price, "strength": poc_strength, "touches": 1, "method": "volume_poc"})

    # VAL как поддержка
    if val_low < current_price:
        supports.append({"price": val_low, "strength": 0.7, "touches": 1, "method": "volume_val"})

    # VAH как сопротивление
    if val_high > current_price:
        resistances.append({"price": val_high, "strength": 0.7, "touches": 1, "method": "volume_vah"})

    return {
        "supports": supports,
        "resistances": resistances,
        "poc": poc_price,
        "val_low": val_low,
        "val_high": val_high,
    }

# ============================================================
# CONFLUENCE — ОБЪЕДИНЯЕМ ВСЕ ТРИ МЕТОДА
# ============================================================

def calc_confluence(local: dict, pivots: dict, volume: dict,
                    current_price: float) -> dict:
    """
    Объединяет уровни из трёх методов.
    Confluence score = сумма весов методов которые подтверждают уровень.

    Веса методов:
    - Volume Profile: 0.45 (самый надёжный — реальный объём)
    - Local min/max:  0.35 (технический анализ)
    - Pivot Points:   0.20 (классика, все знают эти уровни)
    """
    METHOD_WEIGHTS = {
        "volume_poc": 0.45,
        "volume_val": 0.35,
        "volume_vah": 0.35,
        "local_min": 0.35,
        "local_max": 0.35,
        "pivot_s1": 0.20,
        "pivot_s2": 0.15,
        "pivot_s3": 0.10,
        "pivot_r1": 0.20,
        "pivot_r2": 0.15,
        "pivot_r3": 0.10,
    }

    all_supports = (local.get("supports", []) +
                    pivots.get("supports", []) +
                    volume.get("supports", []))
    all_resistances = (local.get("resistances", []) +
                       pivots.get("resistances", []) +
                       volume.get("resistances", []))

    def merge_levels(levels: list) -> list:
        """Объединяет уровни от разных методов в confluence зоны."""
        if not levels:
            return []

        sorted_levels = sorted(levels, key=lambda x: x["price"])
        zones = []
        current_zone = [sorted_levels[0]]

        for level in sorted_levels[1:]:
            last = current_zone[-1]["price"]
            # Группируем уровни в пределах 1%
            if abs(level["price"] - last) / last * 100 <= 1.0:
                current_zone.append(level)
            else:
                zones.append(current_zone)
                current_zone = [level]
        zones.append(current_zone)

        result = []
        for zone in zones:
            avg_price = np.mean([l["price"] for l in zone])
            methods = list({l["method"] for l in zone})

            # Считаем confluence score
            confluence_score = sum(
                METHOD_WEIGHTS.get(l["method"], 0.1) * l["strength"]
                for l in zone
            )
            # Бонус за совпадение нескольких методов
            unique_method_types = len({l["method"].split("_")[0] for l in zone})
            if unique_method_types >= 2:
                confluence_score *= 1.3  # +30% за подтверждение двумя методами
            if unique_method_types >= 3:
                confluence_score *= 1.5  # +50% за подтверждение тремя методами

            result.append({
                "price": round(avg_price, 8),
                "confluence_score": round(min(1.0, confluence_score), 3),
                "methods": methods,
                "method_count": unique_method_types,
                "touches": sum(l.get("touches", 1) for l in zone),
            })

        return sorted(result, key=lambda x: -x["confluence_score"])

    supports = merge_levels(all_supports)
    resistances = merge_levels(all_resistances)

    # Фильтруем — поддержки ниже цены, сопротивления выше
    supports = [s for s in supports if s["price"] < current_price * 0.999]
    resistances = [r for r in resistances if r["price"] > current_price * 1.001]

    # Ближайшие уровни
    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None

    # Определяем сигнал
    signal = "neutral"
    signal_strength = 0.0

    if nearest_support and nearest_resistance:
        dist_to_sup = (current_price - nearest_support["price"]) / current_price * 100
        dist_to_res = (nearest_resistance["price"] - current_price) / current_price * 100

        # Цена у поддержки
        if dist_to_sup <= 1.5 and nearest_support["confluence_score"] >= 0.3:
            signal = "bounce_support"
            signal_strength = nearest_support["confluence_score"] * (1 - dist_to_sup/3)

        # Цена у сопротивления
        elif dist_to_res <= 1.5 and nearest_resistance["confluence_score"] >= 0.3:
            signal = "bounce_resistance"
            signal_strength = nearest_resistance["confluence_score"] * (1 - dist_to_res/3)

        # Пробой вверх
        elif current_price > nearest_resistance["price"] * 0.999:
            signal = "breakout_up"
            signal_strength = nearest_resistance["confluence_score"] * 0.8

        # Пробой вниз
        elif current_price < nearest_support["price"] * 1.001:
            signal = "breakout_down"
            signal_strength = nearest_support["confluence_score"] * 0.8

        # Risk/Reward
        if nearest_support and nearest_resistance:
            risk = dist_to_sup
            reward_long = dist_to_res
            reward_short = dist_to_sup
            rr_long = reward_long / risk if risk > 0 else 0
            rr_short = reward_short / dist_to_res if dist_to_res > 0 else 0
        else:
            rr_long = rr_short = 0
    else:
        rr_long = rr_short = 0
        dist_to_sup = dist_to_res = 0

    return {
        "supports": supports[:5],      # топ 5 уровней поддержки
        "resistances": resistances[:5], # топ 5 уровней сопротивления
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "signal": signal,
        "signal_strength": round(signal_strength, 3),
        "dist_to_support_pct": round(dist_to_sup, 3) if nearest_support else None,
        "dist_to_resistance_pct": round(dist_to_res, 3) if nearest_resistance else None,
        "rr_long": round(rr_long, 2),   # Risk/Reward для LONG
        "rr_short": round(rr_short, 2), # Risk/Reward для SHORT
        "pivot": pivots.get("pivot"),
        "poc": volume.get("poc"),
    }

# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================

async def analyze_sr(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    """
    Полный S/R анализ для символа.
    Возвращает confluence зоны и торговые сигналы.
    """
    # Получаем 200 часовых свечей (≈8 дней)
    candles = await fetch_klines_full(session, symbol, interval="60", limit=200)
    if len(candles) < 50:
        return None

    current_price = candles[-1]["close"]

    # Запускаем все три метода
    local = find_local_extremes(candles, lookback=5)
    pivots = calc_pivot_points(candles)
    volume_profile = calc_volume_profile(candles, bins=100)

    # Объединяем в confluence
    result = calc_confluence(local, pivots, volume_profile, current_price)
    result["symbol"] = symbol
    result["price"] = current_price
    result["analyzed_at"] = datetime.now(timezone.utc)

    return result

def get_sr_entry_signal(sr: dict, direction: str) -> tuple[bool, float, str]:
    """
    Определяет стоит ли входить в сделку на основе S/R анализа.
    Возвращает (should_enter, stop_loss_price, reason).

    Логика:
    - LONG: входим у поддержки, стоп ниже уровня
    - SHORT: входим у сопротивления, стоп выше уровня
    - Минимальный R/R = 1.5
    """
    if not sr:
        return False, 0.0, "no_sr_data"

    signal = sr.get("signal", "neutral")
    strength = float(sr.get("signal_strength") or 0)
    current_price = float(sr.get("price") or 0)

    if current_price <= 0:
        return False, 0.0, "invalid_price"

    if direction == "long":
        if signal not in ("bounce_support", "breakout_up"):
            return False, 0.0, f"sr_no_long_signal({signal})"
        if strength < 0.2:
            return False, 0.0, f"sr_weak({strength:.2f}<0.2)"

        # R/R должен быть минимум 1.5
        rr = float(sr.get("rr_long") or 0)
        if rr < 1.5 and signal != "breakout_up":
            return False, 0.0, f"sr_rr_low({rr:.1f}<1.5)"

        # Стоп — 0.5% ниже уровня поддержки
        support = sr.get("nearest_support")
        if support:
            sl_price = float(support["price"]) * 0.995
        else:
            sl_price = current_price * 0.975

        return True, sl_price, f"sr_long({signal},str={strength:.2f},rr={rr:.1f})"

    elif direction == "short":
        if signal not in ("bounce_resistance", "breakout_down"):
            return False, 0.0, f"sr_no_short_signal({signal})"
        if strength < 0.2:
            return False, 0.0, f"sr_weak({strength:.2f}<0.2)"

        rr = float(sr.get("rr_short") or 0)
        if rr < 1.5 and signal != "breakout_down":
            return False, 0.0, f"sr_rr_low({rr:.1f}<1.5)"

        # Стоп — 0.5% выше уровня сопротивления
        resistance = sr.get("nearest_resistance")
        if resistance:
            sl_price = float(resistance["price"]) * 1.005
        else:
            sl_price = current_price * 1.025

        return True, sl_price, f"sr_short({signal},str={strength:.2f},rr={rr:.1f})"

    return False, 0.0, "invalid_direction"


# ============================================================
# ИНТЕГРАЦИЯ С FEATURES (сохранение в БД)
# ============================================================

async def update_features_sr(symbol: str, sr: dict):
    """Обновляет S/R поля в crypto_features_hourly."""
    import db
    if not sr:
        return

    nearest_sup = sr.get("nearest_support")
    nearest_res = sr.get("nearest_resistance")

    await db.execute(
        """UPDATE crypto_features_hourly SET
           support_1 = $1,
           resistance_1 = $2,
           sr_signal = $3,
           sr_strength = $4
           WHERE symbol = $5
           AND ts = (SELECT MAX(ts) FROM crypto_features_hourly WHERE symbol = $5)""",
        float(nearest_sup["price"]) if nearest_sup else None,
        float(nearest_res["price"]) if nearest_res else None,
        sr.get("signal", "neutral"),
        float(sr.get("signal_strength") or 0) * 100,  # в % для совместимости
        symbol
    )

"""
S/R Engine v2 — профессиональный анализ уровней поддержки и сопротивления.

Методы:
1. Горизонтальные уровни (High/Low) — базовый метод
2. Психологические уровни — круглые числа
3. Скользящие средние (MA50, MA200) — динамические уровни
4. Fibonacci retracement — уровни от последнего движения
5. Pivot Points — классические дневные уровни
6. Volume Profile (POC, VAH, VAL) — объёмные уровни

Confluence score — чем больше методов подтверждают уровень, тем он надёжнее.
"""

import asyncio
import aiohttp
import logging
import numpy as np
from datetime import datetime, timezone
from config import bybit_symbol

logger = logging.getLogger("sr_engine_v2")

BYBIT_BASE = "https://api.bybit.com"

# ============================================================
# ДАННЫЕ
# ============================================================

async def fetch_klines(session: aiohttp.ClientSession, symbol: str,
                       interval: str = "60", limit: int = 200) -> list:
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
            return [{"open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                     "close": float(r[4]), "volume": float(r[5])}
                    for r in reversed(data["result"]["list"])]
    except Exception as e:
        logger.debug(f"Klines fetch error {symbol}: {e}")
        return []

# ============================================================
# МЕТОД 1: ГОРИЗОНТАЛЬНЫЕ УРОВНИ (High/Low)
# ============================================================

def find_horizontal_levels(candles: list, current_price: float) -> dict:
    """
    Находит значимые горизонтальные уровни.
    Использует HIGH и LOW свечей с большим lookback.
    Учитывает количество касаний — сильный уровень касался 3+ раза.
    """
    if len(candles) < 20:
        return {"supports": [], "resistances": []}

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]
    avg_vol = np.mean(volumes) if volumes else 1
    min_dist = current_price * 0.003  # минимум 0.3% от цены

    LOOKBACK = min(10, len(candles) // 4)

    raw_supports = []
    raw_resistances = []

    for i in range(LOOKBACK, len(candles) - LOOKBACK):
        # Локальный минимум
        if (all(lows[i] <= lows[i-j] for j in range(1, LOOKBACK+1)) and
                all(lows[i] <= lows[i+j] for j in range(1, LOOKBACK+1))):
            # Касания в пределах 0.5%
            touches = sum(1 for j in range(len(candles))
                         if abs(lows[j] - lows[i]) / max(lows[i], 0.0001) < 0.005)
            vol_w = volumes[i] / avg_vol
            raw_supports.append({"price": lows[i], "strength": touches + vol_w, "method": "horizontal"})

        # Локальный максимум
        if (all(highs[i] >= highs[i-j] for j in range(1, LOOKBACK+1)) and
                all(highs[i] >= highs[i+j] for j in range(1, LOOKBACK+1))):
            touches = sum(1 for j in range(len(candles))
                         if abs(highs[j] - highs[i]) / max(highs[i], 0.0001) < 0.005)
            vol_w = volumes[i] / avg_vol
            raw_resistances.append({"price": highs[i], "strength": touches + vol_w, "method": "horizontal"})

    def cluster(levels, tol=0.005):
        if not levels:
            return []
        levels = sorted(levels, key=lambda x: x["price"])
        clusters, cur = [], [levels[0]]
        for lv in levels[1:]:
            if abs(lv["price"] - cur[-1]["price"]) / max(cur[-1]["price"], 0.0001) <= tol:
                cur.append(lv)
            else:
                clusters.append(cur)
                cur = [lv]
        clusters.append(cur)
        result = []
        for cl in clusters:
            result.append({
                "price": round(np.mean([l["price"] for l in cl]), 8),
                "strength": sum(l["strength"] for l in cl),
                "method": "horizontal",
                "touches": len(cl)
            })
        return sorted(result, key=lambda x: -x["strength"])

    supports = [s for s in cluster(raw_supports) if current_price - s["price"] >= min_dist]
    resistances = [r for r in cluster(raw_resistances) if r["price"] - current_price >= min_dist]

    return {"supports": supports[:5], "resistances": resistances[:5]}

# ============================================================
# МЕТОД 2: ПСИХОЛОГИЧЕСКИЕ УРОВНИ
# ============================================================

def find_psychological_levels(current_price: float) -> dict:
    """
    Круглые числа — трейдеры инстинктивно ставят ордера на них.
    Чем больше нулей — тем сильнее уровень.
    BTC $70,000 >> $70,500 >> $70,100
    """
    if current_price <= 0:
        return {"supports": [], "resistances": []}

    # Определяем шаг в зависимости от цены
    if current_price >= 10000:
        steps = [1000, 5000, 10000]
    elif current_price >= 1000:
        steps = [100, 500, 1000]
    elif current_price >= 100:
        steps = [10, 50, 100]
    elif current_price >= 10:
        steps = [1, 5, 10]
    elif current_price >= 1:
        steps = [0.1, 0.5, 1.0]
    elif current_price >= 0.1:
        steps = [0.01, 0.05, 0.1]
    elif current_price >= 0.01:
        steps = [0.001, 0.005, 0.01]
    else:
        steps = [current_price * 0.1, current_price * 0.5, current_price * 1.0]

    levels = set()
    for step in steps:
        base = round(current_price / step) * step
        for mult in range(-5, 6):
            lv = round(base + mult * step, 10)
            if lv > 0 and abs(lv - current_price) / current_price > 0.001:
                levels.add(lv)

    # Сила уровня зависит от "круглости"
    def roundness(price):
        score = 1.0
        for step in steps:
            if abs(price % step) / max(step, 0.000001) < 0.001:
                score += steps.index(step) + 1
        return score

    supports = []
    resistances = []
    min_dist = current_price * 0.002

    for lv in sorted(levels):
        if lv < current_price - min_dist:
            supports.append({"price": lv, "strength": roundness(lv) * 2, "method": "psychological"})
        elif lv > current_price + min_dist:
            resistances.append({"price": lv, "strength": roundness(lv) * 2, "method": "psychological"})

    # Ближайшие 5 в каждую сторону
    supports = sorted(supports, key=lambda x: -x["price"])[:5]
    resistances = sorted(resistances, key=lambda x: x["price"])[:5]

    return {"supports": supports, "resistances": resistances}

# ============================================================
# МЕТОД 3: СКОЛЬЗЯЩИЕ СРЕДНИЕ
# ============================================================

def find_ma_levels(candles: list, current_price: float) -> dict:
    """
    MA50, MA100, MA200 как динамические уровни поддержки/сопротивления.
    MA200 — самый важный уровень на любом рынке.
    """
    if not candles:
        return {"supports": [], "resistances": []}

    closes = [c["close"] for c in candles]
    min_dist = current_price * 0.001

    supports = []
    resistances = []

    ma_configs = [
        (20, 1.5, "MA20"),
        (50, 2.5, "MA50"),
        (100, 3.0, "MA100"),
        (200, 4.0, "MA200"),
    ]

    for period, weight, name in ma_configs:
        if len(closes) < period:
            continue
        ma = np.mean(closes[-period:])

        if ma < current_price - min_dist:
            supports.append({"price": round(ma, 8), "strength": weight, "method": name})
        elif ma > current_price + min_dist:
            resistances.append({"price": round(ma, 8), "strength": weight, "method": name})

    return {"supports": supports, "resistances": resistances}

# ============================================================
# МЕТОД 4: FIBONACCI RETRACEMENT
# ============================================================

def find_fibonacci_levels(candles: list, current_price: float) -> dict:
    """
    Fibonacci retracement от последнего значимого движения.
    Уровни: 23.6%, 38.2%, 50%, 61.8%, 78.6%
    
    Ищем последний swing high и swing low за 50-100 свечей.
    """
    if len(candles) < 20:
        return {"supports": [], "resistances": []}

    FIB_LEVELS = [0.236, 0.382, 0.5, 0.618, 0.786]
    FIB_WEIGHTS = {0.236: 1.0, 0.382: 2.0, 0.5: 1.5, 0.618: 3.0, 0.786: 2.0}  # 61.8% - "золотое сечение"

    recent = candles[-100:] if len(candles) >= 100 else candles
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]

    swing_high = max(highs)
    swing_low = min(lows)
    move = swing_high - swing_low

    if move <= 0 or move / current_price < 0.01:  # движение менее 1% — игнорируем
        return {"supports": [], "resistances": []}

    min_dist = current_price * 0.002
    supports = []
    resistances = []

    # Retracement уровни (от движения вниз — поддержки, от движения вверх — сопротивления)
    for fib in FIB_LEVELS:
        # Уровни снизу вверх
        level_up = swing_low + move * fib
        # Уровни сверху вниз
        level_down = swing_high - move * fib

        weight = FIB_WEIGHTS.get(fib, 1.0)

        for lv in [level_up, level_down]:
            lv = round(lv, 8)
            if lv < current_price - min_dist:
                supports.append({"price": lv, "strength": weight, "method": f"fib_{fib}"})
            elif lv > current_price + min_dist:
                resistances.append({"price": lv, "strength": weight, "method": f"fib_{fib}"})

    # Убираем дубли
    def dedup(levels, tol=0.003):
        seen = []
        for lv in sorted(levels, key=lambda x: x["price"]):
            if not seen or abs(lv["price"] - seen[-1]["price"]) / max(seen[-1]["price"], 0.0001) > tol:
                seen.append(lv)
            else:
                # Объединяем
                seen[-1]["strength"] = max(seen[-1]["strength"], lv["strength"])
        return seen

    return {"supports": dedup(supports)[:5], "resistances": dedup(resistances)[:5]}

# ============================================================
# МЕТОД 5: PIVOT POINTS
# ============================================================

def find_pivot_points(candles: list, current_price: float) -> dict:
    """
    Классические Pivot Points из последних 24 часов.
    Все крупные трейдеры знают эти уровни → самоисполняющееся пророчество.
    """
    if len(candles) < 24:
        return {"supports": [], "resistances": [], "pivot": None}

    last_day = candles[-24:]
    high = max(c["high"] for c in last_day)
    low = min(c["low"] for c in last_day)
    close = last_day[-1]["close"]

    p = (high + low + close) / 3
    r1 = 2*p - low
    r2 = p + (high - low)
    r3 = high + 2*(p - low)
    s1 = 2*p - high
    s2 = p - (high - low)
    s3 = low - 2*(high - p)

    min_dist = current_price * 0.001

    resistances = [
        {"price": round(r1, 8), "strength": 3.0, "method": "pivot_r1"},
        {"price": round(r2, 8), "strength": 2.0, "method": "pivot_r2"},
        {"price": round(r3, 8), "strength": 1.0, "method": "pivot_r3"},
    ]
    supports = [
        {"price": round(s1, 8), "strength": 3.0, "method": "pivot_s1"},
        {"price": round(s2, 8), "strength": 2.0, "method": "pivot_s2"},
        {"price": round(s3, 8), "strength": 1.0, "method": "pivot_s3"},
    ]

    supports = [s for s in supports if s["price"] > 0 and current_price - s["price"] >= min_dist]
    resistances = [r for r in resistances if r["price"] > 0 and r["price"] - current_price >= min_dist]

    return {"supports": supports, "resistances": resistances, "pivot": round(p, 8)}

# ============================================================
# МЕТОД 6: VOLUME PROFILE
# ============================================================

def find_volume_profile(candles: list, current_price: float, bins: int = 100) -> dict:
    """
    Volume Profile — где реально торговали.
    POC (Point of Control) — самый сильный уровень.
    VAH/VAL — Value Area High/Low (70% объёма).
    """
    if len(candles) < 20 or current_price <= 0:
        return {"supports": [], "resistances": [], "poc": None}

    prices_all = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles]
    volumes_all = [c["volume"] for c in candles]

    min_p = min(c["low"] for c in candles)
    max_p = max(c["high"] for c in candles)

    if min_p >= max_p:
        return {"supports": [], "resistances": [], "poc": None}

    bin_size = (max_p - min_p) / bins
    bin_volumes = [0.0] * bins
    bin_prices = [min_p + (i + 0.5) * bin_size for i in range(bins)]

    for price, volume in zip(prices_all, volumes_all):
        idx = int((price - min_p) / bin_size)
        idx = max(0, min(bins - 1, idx))
        bin_volumes[idx] += volume

    poc_idx = bin_volumes.index(max(bin_volumes))
    poc_price = bin_prices[poc_idx]

    # Value Area
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
                low_idx -= 1; accumulated += bin_volumes[low_idx]
            else:
                high_idx += 1; accumulated += bin_volumes[high_idx]
        elif expand_low:
            low_idx -= 1; accumulated += bin_volumes[low_idx]
        else:
            high_idx += 1; accumulated += bin_volumes[high_idx]

    val = bin_prices[low_idx]
    vah = bin_prices[high_idx]
    min_dist = current_price * 0.001

    supports = []
    resistances = []

    for price, strength, name in [(poc_price, 4.0, "vol_poc"), (val, 2.5, "vol_val"), (vah, 2.5, "vol_vah")]:
        price = round(price, 8)
        if price < current_price - min_dist:
            supports.append({"price": price, "strength": strength, "method": name})
        elif price > current_price + min_dist:
            resistances.append({"price": price, "strength": strength, "method": name})

    return {"supports": supports, "resistances": resistances, "poc": round(poc_price, 8)}

# ============================================================
# CONFLUENCE — ОБЪЕДИНЯЕМ ВСЕ МЕТОДЫ
# ============================================================

def classify_sr_signal(candles: list, current_price: float, nearest_support: dict | None, nearest_resistance: dict | None) -> tuple[str, float]:
    """
    Отличает обычный bounce от пробоя с ретестом.
    """
    if not candles or len(candles) < 8 or current_price <= 0:
        return "neutral", 0.0

    closes = [float(c["close"]) for c in candles[-8:]]
    highs = [float(c["high"]) for c in candles[-8:]]
    lows = [float(c["low"]) for c in candles[-8:]]

    signal = "neutral"
    strength_mult = 1.0

    if nearest_support:
        sup = float(nearest_support["price"])
        dist_sup = (current_price - sup) / current_price * 100

        was_above_sup = any(cl > sup * 1.001 for cl in closes[:-2])
        broke_below_sup = any(cl < sup * 0.999 for cl in closes[-4:])
        retested_sup_from_below = (
            current_price < sup
            and dist_sup >= 0
            and dist_sup <= 2.0
            and max(highs[-4:]) >= sup * 0.999
            and closes[-1] <= sup * 1.001
        )

        if was_above_sup and broke_below_sup and retested_sup_from_below:
            return "retest_broken_support_short", 1.15

    if nearest_resistance:
        res = float(nearest_resistance["price"])
        dist_res = (res - current_price) / current_price * 100

        was_below_res = any(cl < res * 0.999 for cl in closes[:-2])
        broke_above_res = any(cl > res * 1.001 for cl in closes[-4:])
        retested_res_from_above = (
            current_price > res
            and dist_res <= 0
            and abs(dist_res) <= 2.0
            and min(lows[-4:]) <= res * 1.001
            and closes[-1] >= res * 0.999
        )

        if was_below_res and broke_above_res and retested_res_from_above:
            return "retest_broken_resistance_long", 1.15

    if nearest_support:
        sup = float(nearest_support["price"])
        dist_sup = (current_price - sup) / current_price * 100
        if 0 <= dist_sup <= 2.0:
            return "bounce_support", 1.0
        if current_price < sup:
            return "breakout_down", 0.9

    if nearest_resistance:
        res = float(nearest_resistance["price"])
        dist_res = (res - current_price) / current_price * 100
        if 0 <= dist_res <= 2.0:
            return "bounce_resistance", 1.0
        if current_price > res:
            return "breakout_up", 0.9

    return signal, strength_mult


# Веса методов
METHOD_WEIGHTS = {
    "horizontal": 0.35,   # реальные уровни от торгов
    "vol_poc":    0.25,   # Point of Control — самый торгуемый уровень
    "vol_val":    0.15,
    "vol_vah":    0.15,
    "fib_0.618":  0.20,   # золотое сечение
    "fib_0.382":  0.15,
    "fib_0.5":    0.12,
    "fib_0.236":  0.08,
    "fib_0.786":  0.10,
    "MA200":      0.30,   # самая важная MA
    "MA100":      0.20,
    "MA50":       0.15,
    "MA20":       0.08,
    "pivot_r1":   0.15,   # все знают pivot points
    "pivot_s1":   0.15,
    "pivot_r2":   0.10,
    "pivot_s2":   0.10,
    "pivot_r3":   0.05,
    "pivot_s3":   0.05,
    "psychological": 0.15, # круглые числа
}

def calc_confluence(all_levels: list, current_price: float, candles: list | None = None) -> dict:
    """
    Объединяет уровни из всех методов.
    Confluence score = сумма весов методов которые подтверждают уровень.
    Бонус за совпадение нескольких методов (+30% за 2, +60% за 3+).
    """
    if not all_levels:
        return {"supports": [], "resistances": [], "nearest_support": None,
                "nearest_resistance": None, "signal": "neutral", "signal_strength": 0.0}

    # Кластеризуем все уровни в пределах 0.8%
    sorted_levels = sorted(all_levels, key=lambda x: x["price"])
    clusters = []
    cur = [sorted_levels[0]]

    for lv in sorted_levels[1:]:
        if abs(lv["price"] - cur[-1]["price"]) / max(cur[-1]["price"], 0.0001) <= 0.008:
            cur.append(lv)
        else:
            clusters.append(cur)
            cur = [lv]
    clusters.append(cur)

    result = []
    for cluster in clusters:
        avg_price = np.mean([l["price"] for l in cluster])
        methods = list({l["method"] for l in cluster})

        # Базовый score
        score = sum(
            METHOD_WEIGHTS.get(l["method"], 0.05) * l.get("strength", 1.0)
            for l in cluster
        )

        # Бонус за confluence (несколько методов на одном уровне)
        method_types = len({l["method"].split("_")[0] for l in cluster})
        if method_types >= 3:
            score *= 1.6
        elif method_types >= 2:
            score *= 1.3

        result.append({
            "price": round(avg_price, 8),
            "confluence_score": round(min(1.0, score), 3),
            "methods": methods,
            "method_count": method_types,
        })

    # Разделяем на поддержки и сопротивления
    min_dist = current_price * 0.001
    supports = sorted(
        [r for r in result if r["price"] < current_price - min_dist],
        key=lambda x: -x["price"]  # ближайшие первые
    )
    resistances = sorted(
        [r for r in result if r["price"] > current_price + min_dist],
        key=lambda x: x["price"]  # ближайшие первые
    )

    # Сортируем по силе в пределах первых 3
    supports = sorted(supports[:8], key=lambda x: -x["confluence_score"])
    resistances = sorted(resistances[:8], key=lambda x: -x["confluence_score"])

    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None

    # Сигнал
    signal = "neutral"
    signal_strength = 0.0

    signal, strength_mult = classify_sr_signal(
        candles=candles or [],
        current_price=current_price,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
    )

    # R/R для входа
    rr_short = rr_long = 0.0
    if nearest_support and nearest_resistance:
        dist_sup = (current_price - nearest_support["price"]) / current_price * 100
        dist_res = (nearest_resistance["price"] - current_price) / current_price * 100
        if dist_sup > 0:
            rr_long = dist_res / (dist_sup * 0.3 + 0.1)  # стоп ~0.3% за уровень
        if dist_res > 0:
            rr_short = dist_sup / (dist_res * 0.3 + 0.1)

    if signal == "bounce_support" and nearest_support:
        dist_sup = max(0.0, (current_price - nearest_support["price"]) / current_price * 100)
        signal_strength = nearest_support["confluence_score"] * (1 - dist_sup / 4) * 100 * strength_mult
    elif signal == "bounce_resistance" and nearest_resistance:
        dist_res = max(0.0, (nearest_resistance["price"] - current_price) / current_price * 100)
        signal_strength = nearest_resistance["confluence_score"] * (1 - dist_res / 4) * 100 * strength_mult
    elif signal == "retest_broken_support_short" and nearest_support:
        signal_strength = nearest_support["confluence_score"] * 85 * strength_mult
    elif signal == "retest_broken_resistance_long" and nearest_resistance:
        signal_strength = nearest_resistance["confluence_score"] * 85 * strength_mult
    elif signal == "breakout_up" and nearest_resistance:
        signal_strength = nearest_resistance["confluence_score"] * 70 * strength_mult
    elif signal == "breakout_down" and nearest_support:
        signal_strength = nearest_support["confluence_score"] * 70 * strength_mult

    return {
        "supports": supports[:5],
        "resistances": resistances[:5],
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "signal": signal,
        "signal_strength": round(min(100, signal_strength), 1),
        "rr_long": round(rr_long, 2),
        "rr_short": round(rr_short, 2),
    }

# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================

async def analyze_sr_v2(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    """
    Полный S/R анализ с 6 методами.
    Возвращает confluence зоны и торговые сигналы.
    """
    # 200 свечей 1h для основного анализа
    candles_1h = await fetch_klines(session, symbol, interval="60", limit=200)
    if len(candles_1h) < 20:
        return None

    current_price = candles_1h[-1]["close"]
    if current_price <= 0:
        return None

    # 200 свечей 1D для значимых уровней (второй слой)
    candles_1d = await fetch_klines(session, symbol, interval="D", limit=200)

    # Запускаем все методы на 1h
    horizontal = find_horizontal_levels(candles_1h, current_price)
    psychological = find_psychological_levels(current_price)
    ma_levels = find_ma_levels(candles_1h, current_price)
    fibonacci = find_fibonacci_levels(candles_1h, current_price)
    pivots = find_pivot_points(candles_1h, current_price)
    volume = find_volume_profile(candles_1h, current_price)

    # Дополнительные уровни с дневного таймфрейма (более значимые)
    horizontal_1d = {"supports": [], "resistances": []}
    ma_levels_1d = {"supports": [], "resistances": []}
    fibonacci_1d = {"supports": [], "resistances": []}
    volume_1d = {"supports": [], "resistances": []}

    if candles_1d and len(candles_1d) >= 20:
        horizontal_1d = find_horizontal_levels(candles_1d, current_price)
        ma_levels_1d = find_ma_levels(candles_1d, current_price)
        fibonacci_1d = find_fibonacci_levels(candles_1d, current_price)
        volume_1d = find_volume_profile(candles_1d, current_price)
        for lv in horizontal_1d["supports"] + horizontal_1d["resistances"]:
            lv["strength"] *= 2.0
            lv["method"] = "horizontal_1d"
        for lv in ma_levels_1d["supports"] + ma_levels_1d["resistances"]:
            lv["strength"] *= 2.0
            lv["method"] = lv["method"] + "_1d"
        for lv in fibonacci_1d["supports"] + fibonacci_1d["resistances"]:
            lv["strength"] *= 1.5
            lv["method"] = lv["method"] + "_1d"
        for lv in volume_1d["supports"] + volume_1d["resistances"]:
            lv["strength"] *= 1.8
            lv["method"] = lv["method"] + "_1d"

    # Объединяем все уровни (1h + 1D)
    all_levels = (
        horizontal["supports"] + horizontal["resistances"] +
        psychological["supports"] + psychological["resistances"] +
        ma_levels["supports"] + ma_levels["resistances"] +
        fibonacci["supports"] + fibonacci["resistances"] +
        pivots["supports"] + pivots["resistances"] +
        volume["supports"] + volume["resistances"] +
        horizontal_1d["supports"] + horizontal_1d["resistances"] +
        ma_levels_1d["supports"] + ma_levels_1d["resistances"] +
        fibonacci_1d["supports"] + fibonacci_1d["resistances"] +
        volume_1d["supports"] + volume_1d["resistances"]
    )

    result = calc_confluence(all_levels, current_price, candles_1h)
    result["symbol"] = symbol
    result["price"] = current_price
    result["poc"] = volume.get("poc")
    result["pivot"] = pivots.get("pivot")
    result["analyzed_at"] = datetime.now(timezone.utc)

    return result

def get_sr_entry_signal(sr: dict, direction: str) -> tuple[bool, float, str]:
    """
    Определяет стоит ли входить на основе S/R анализа.
    Возвращает (should_enter, stop_loss_price, reason).
    Минимальный R/R = 1.5
    """
    if not sr:
        return False, 0.0, "no_sr_data"

    signal = sr.get("signal", "neutral")
    strength = float(sr.get("signal_strength") or 0)
    current = float(sr.get("price") or 0)

    if current <= 0:
        return False, 0.0, "invalid_price"

    if direction == "long":
        if signal not in ("bounce_support", "breakout_up", "retest_broken_resistance_long"):
            return False, 0.0, f"sr_no_long({signal})"
        if strength < 15:
            return False, 0.0, f"sr_weak({strength:.0f}<15)"
        rr = float(sr.get("rr_long") or 0)
        if rr < 1.5 and signal != "breakout_up":
            return False, 0.0, f"sr_rr_low({rr:.1f})"
        support = sr.get("nearest_support")
        sl = float(support["price"]) * 0.995 if support else current * 0.975
        return True, sl, f"sr_long({signal},str={strength:.0f},rr={rr:.1f})"

    elif direction == "short":
        if signal not in ("bounce_resistance", "breakout_down", "retest_broken_support_short"):
            return False, 0.0, f"sr_no_short({signal})"
        if strength < 15:
            return False, 0.0, f"sr_weak({strength:.0f}<15)"
        rr = float(sr.get("rr_short") or 0)
        if rr < 1.5 and signal != "breakout_down":
            return False, 0.0, f"sr_rr_low({rr:.1f})"
        resistance = sr.get("nearest_resistance")
        sl = float(resistance["price"]) * 1.005 if resistance else current * 1.025
        return True, sl, f"sr_short({signal},str={strength:.0f},rr={rr:.1f})"

    return False, 0.0, "invalid_direction"


# ============================================================
# АЛИАСЫ ДЛЯ СОВМЕСТИМОСТИ С trader.py
# ============================================================

async def analyze_sr(session, symbol: str) -> dict | None:
    """Алиас для analyze_sr_v2 — совместимость с trader.py."""
    return await analyze_sr_v2(session, symbol)

async def update_features_sr(symbol: str, sr_data: dict) -> None:
    """Обновляет S/R данные в features таблице — совместимость с trader.py."""
    if not sr_data:
        return
    try:
        import db
        await db.execute(
            """UPDATE crypto_features_hourly
               SET support_1=$1, resistance_1=$2, sr_signal=$3, sr_strength=$4
               WHERE symbol=$5 AND ts=(SELECT MAX(ts) FROM crypto_features_hourly WHERE symbol=$5)""",
            sr_data.get("nearest_support", {}).get("price") if sr_data.get("nearest_support") else None,
            sr_data.get("nearest_resistance", {}).get("price") if sr_data.get("nearest_resistance") else None,
            sr_data.get("signal", "neutral"),
            float(sr_data.get("signal_strength") or 0),
            symbol
        )
    except Exception as e:
        import logging
        logging.getLogger("sr_engine_v2").warning(f"update_features_sr {symbol}: {e}")

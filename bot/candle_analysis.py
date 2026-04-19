"""
Candle Analysis Module — FVG, Order Blocks, Market Structure, Fibonacci
Полностью симметричный для лонгов и шортов.
"""


def detect_fvg(candles: list, current_price: float) -> dict:
    """Fair Value Gap — зоны дисбаланса."""
    result = {
        "bullish_fvg": None, "bearish_fvg": None,
        "in_bullish_fvg": False, "in_bearish_fvg": False,
        "nearest_fvg": None, "nearest_fvg_dist_pct": None,
    }
    if len(candles) < 3 or current_price <= 0:
        return result

    bullish_fvgs, bearish_fvgs = [], []
    for i in range(2, min(30, len(candles))):
        c0 = candles[-(i+1)]
        c2 = candles[-(i-1)]
        high0, low0 = float(c0["high"]), float(c0["low"])
        high2, low2 = float(c2["high"]), float(c2["low"])

        if low2 > high0:
            gap = (low2 - high0) / current_price * 100
            if gap >= 0.05:
                mid = (low2 + high0) / 2
                dist = abs(current_price - mid) / current_price * 100
                in_fvg = high0 <= current_price <= low2
                bullish_fvgs.append({"top": round(low2,8), "bottom": round(high0,8),
                    "mid": round(mid,8), "size_pct": round(gap,3),
                    "dist_pct": round(dist,3), "in_fvg": in_fvg, "ago": i})

        if high2 < low0:
            gap = (low0 - high2) / current_price * 100
            if gap >= 0.05:
                mid = (low0 + high2) / 2
                dist = abs(current_price - mid) / current_price * 100
                in_fvg = high2 <= current_price <= low0
                bearish_fvgs.append({"top": round(low0,8), "bottom": round(high2,8),
                    "mid": round(mid,8), "size_pct": round(gap,3),
                    "dist_pct": round(dist,3), "in_fvg": in_fvg, "ago": i})

    if bullish_fvgs:
        in_fvg = [f for f in bullish_fvgs if f["in_fvg"]]
        result["bullish_fvg"] = in_fvg[0] if in_fvg else sorted(bullish_fvgs, key=lambda x: x["dist_pct"])[0]
        result["in_bullish_fvg"] = bool(in_fvg)

    if bearish_fvgs:
        in_fvg = [f for f in bearish_fvgs if f["in_fvg"]]
        result["bearish_fvg"] = in_fvg[0] if in_fvg else sorted(bearish_fvgs, key=lambda x: x["dist_pct"])[0]
        result["in_bearish_fvg"] = bool(in_fvg)

    candidates = []
    if result["bullish_fvg"]: candidates.append(("bullish", result["bullish_fvg"]["dist_pct"]))
    if result["bearish_fvg"]: candidates.append(("bearish", result["bearish_fvg"]["dist_pct"]))
    if candidates:
        nearest = min(candidates, key=lambda x: x[1])
        result["nearest_fvg"] = nearest[0]
        result["nearest_fvg_dist_pct"] = nearest[1]

    return result


def detect_fibonacci(candles: list, current_price: float) -> dict:
    """
    Фибоначчи — определяет уровни отката от последнего значимого движения.

    Алгоритм:
    1. Находим последний swing high и swing low за 50 свечей
    2. Определяем направление движения (импульс вверх или вниз)
    3. Считаем уровни отката: 0.236, 0.382, 0.5, 0.618, 0.786
    4. Определяем в какой зоне сейчас цена (±1.5% от уровня = зона)

    Для лонга: ищем откат вниз после импульса вверх (цена у 0.618 = хорошая точка входа в лонг)
    Для шорта: ищем откат вверх после импульса вниз (цена у 0.618 = хорошая точка входа в шорт)

    Возвращает:
    - fib_level: ближайший уровень (0.236/0.382/0.5/0.618/0.786/None)
    - fib_dist_pct: расстояние до ближайшего уровня в %
    - fib_zone: "golden" (0.618±1.5%), "half" (0.5±1.5%), "shallow" (0.382±1.5%), "deep" (0.786±1.5%), None
    - fib_direction: "bullish_retracement" (откат в бычьем движении) / "bearish_retracement" / None
    - swing_high: последний swing high
    - swing_low: последний swing low
    """
    result = {
        "fib_level": None,
        "fib_dist_pct": None,
        "fib_zone": None,
        "fib_direction": None,
        "swing_high": None,
        "swing_low": None,
        "fib_score_long": 0,
        "fib_score_short": 0,
    }

    if len(candles) < 10 or current_price <= 0:
        return result

    lookback = min(50, len(candles))
    recent = candles[-lookback:]

    # Находим swing high и swing low за последние 50 свечей
    swing_size = 3  # минимум 3 свечи слева и справа для подтверждения
    swing_highs = []
    swing_lows = []

    for i in range(swing_size, len(recent) - swing_size):
        c = recent[i]
        h = float(c["high"])
        l = float(c["low"])

        # Swing high: максимум среди swing_size соседних свечей
        if h == max(float(recent[i+j]["high"]) for j in range(-swing_size, swing_size+1)):
            swing_highs.append({"price": h, "idx": i})

        # Swing low: минимум среди swing_size соседних свечей
        if l == min(float(recent[i+j]["low"]) for j in range(-swing_size, swing_size+1)):
            swing_lows.append({"price": l, "idx": i})

    if not swing_highs or not swing_lows:
        return result

    # Берём последний swing high и swing low
    last_high = swing_highs[-1]
    last_low = swing_lows[-1]

    result["swing_high"] = round(last_high["price"], 8)
    result["swing_low"] = round(last_low["price"], 8)

    swing_range = last_high["price"] - last_low["price"]
    if swing_range <= 0:
        return result

    # Определяем направление последнего импульса
    # Если swing high после swing low → импульс вверх → ищем откат вниз (для лонга)
    # Если swing low после swing high → импульс вниз → ищем откат вверх (для шорта)

    FIB_LEVELS = [0.236, 0.382, 0.5, 0.618, 0.786]
    FIB_ZONE_PCT = 1.5  # ±1.5% от уровня считается зоной

    if last_high["idx"] > last_low["idx"]:
        # Импульс вверх: high после low
        # Откат вниз: уровни считаются от high вниз к low
        # Fib 0.618 = high - 0.618 * range
        result["fib_direction"] = "bullish_retracement"
        fib_prices = {
            level: last_high["price"] - level * swing_range
            for level in FIB_LEVELS
        }
    else:
        # Импульс вниз: low после high
        # Откат вверх: уровни считаются от low вверх к high
        # Fib 0.618 = low + 0.618 * range
        result["fib_direction"] = "bearish_retracement"
        fib_prices = {
            level: last_low["price"] + level * swing_range
            for level in FIB_LEVELS
        }

    # Находим ближайший уровень к текущей цене
    min_dist = float('inf')
    nearest_level = None
    nearest_price = None

    for level, price in fib_prices.items():
        dist_pct = abs(current_price - price) / current_price * 100
        if dist_pct < min_dist:
            min_dist = dist_pct
            nearest_level = level
            nearest_price = price

    result["fib_level"] = nearest_level
    result["fib_dist_pct"] = round(min_dist, 3)

    # Определяем зону (±1.5%)
    if min_dist <= FIB_ZONE_PCT:
        if abs(nearest_level - 0.618) < 0.05:
            result["fib_zone"] = "golden"   # самая сильная зона
        elif abs(nearest_level - 0.5) < 0.05:
            result["fib_zone"] = "half"
        elif abs(nearest_level - 0.382) < 0.05:
            result["fib_zone"] = "shallow"
        elif abs(nearest_level - 0.786) < 0.05:
            result["fib_zone"] = "deep"
        elif abs(nearest_level - 0.236) < 0.05:
            result["fib_zone"] = "weak"

    # Считаем скор для лонга и шорта
    # Для лонга: bullish_retracement + цена у 0.618 = хорошо (цена откатилась и готова расти)
    # Для шорта: bearish_retracement + цена у 0.618 = хорошо (цена откатилась и готова падать)

    zone_scores = {
        "golden": 25,  # 0.618 — самый сильный уровень
        "half":   15,  # 0.5
        "shallow": 8,  # 0.382
        "deep":   10,  # 0.786
        "weak":    5,  # 0.236
    }

    zone_score = zone_scores.get(result["fib_zone"], 0)

    if result["fib_direction"] == "bullish_retracement":
        # Цена откатилась в бычьем тренде — хорошо для лонга
        result["fib_score_long"] = zone_score
        result["fib_score_short"] = -zone_score // 2  # плохо для шорта
    elif result["fib_direction"] == "bearish_retracement":
        # Цена откатилась в медвежьем тренде — хорошо для шорта
        result["fib_score_short"] = zone_score
        result["fib_score_long"] = -zone_score // 2  # плохо для лонга

    return result


def detect_order_blocks(candles: list, current_price: float) -> dict:
    """Order Blocks — зоны крупных позиций."""
    result = {
        "bullish_ob": None, "bearish_ob": None,
        "in_bullish_ob": False, "in_bearish_ob": False,
    }
    if len(candles) < 5 or current_price <= 0:
        return result

    min_impulse = current_price * 0.008

    for i in range(3, min(50, len(candles) - 2)):
        c = candles[-(i+1)]
        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        body = abs(cl - o)
        if body == 0:
            continue

        next_slice = candles[-(i):-(i-2)] if i > 2 else []
        if not next_slice:
            continue

        if cl < o:
            future_high = max(float(nc["high"]) for nc in next_slice)
            if future_high - h >= min_impulse:
                mid = (h + l) / 2
                dist = abs(current_price - mid) / current_price * 100
                in_ob = l <= current_price <= h
                if result["bullish_ob"] is None or dist < result["bullish_ob"]["dist_pct"]:
                    result["bullish_ob"] = {"top": round(h,8), "bottom": round(l,8),
                        "mid": round(mid,8), "dist_pct": round(dist,3), "ago": i}
                    result["in_bullish_ob"] = in_ob

        if cl > o:
            future_low = min(float(nc["low"]) for nc in next_slice)
            if l - future_low >= min_impulse:
                mid = (h + l) / 2
                dist = abs(current_price - mid) / current_price * 100
                in_ob = l <= current_price <= h
                if result["bearish_ob"] is None or dist < result["bearish_ob"]["dist_pct"]:
                    result["bearish_ob"] = {"top": round(h,8), "bottom": round(l,8),
                        "mid": round(mid,8), "dist_pct": round(dist,3), "ago": i}
                    result["in_bearish_ob"] = in_ob

    return result


def detect_market_structure(candles: list, current_price: float) -> dict:
    """Market Structure: uptrend/downtrend/ranging + BOS + CHoCH."""
    result = {
        "structure": "ranging",
        "last_swing_high": None, "last_swing_low": None,
        "bos_bullish": False, "bos_bearish": False,
        "choch_bullish": False, "choch_bearish": False,
    }
    if len(candles) < 20 or current_price <= 0:
        return result

    lookback = 5
    swings = []

    for i in range(lookback, len(candles) - lookback):
        window_highs = [candles[i+j]["high"] for j in range(-lookback, lookback+1)]
        window_lows = [candles[i+j]["low"] for j in range(-lookback, lookback+1)]
        c = candles[i]
        if float(c["high"]) == max(float(x) for x in window_highs):
            swings.append({"type": "high", "price": float(c["high"]), "idx": i})
        elif float(c["low"]) == min(float(x) for x in window_lows):
            swings.append({"type": "low", "price": float(c["low"]), "idx": i})

    if len(swings) < 4:
        return result

    recent_highs = [s for s in swings if s["type"] == "high"][-3:]
    recent_lows = [s for s in swings if s["type"] == "low"][-3:]

    if recent_highs:
        result["last_swing_high"] = recent_highs[-1]["price"]
    if recent_lows:
        result["last_swing_low"] = recent_lows[-1]["price"]

    if len(recent_highs) >= 2 and len(recent_lows) >= 2:
        hh = recent_highs[-1]["price"] > recent_highs[-2]["price"]
        hl = recent_lows[-1]["price"] > recent_lows[-2]["price"]
        lh = recent_highs[-1]["price"] < recent_highs[-2]["price"]
        ll = recent_lows[-1]["price"] < recent_lows[-2]["price"]

        if hh and hl:
            result["structure"] = "uptrend"
        elif lh and ll:
            result["structure"] = "downtrend"

        if result["last_swing_high"] and current_price > result["last_swing_high"]:
            result["bos_bullish"] = True
        if result["last_swing_low"] and current_price < result["last_swing_low"]:
            result["bos_bearish"] = True

        if result["structure"] == "downtrend" and hh:
            result["choch_bullish"] = True
        if result["structure"] == "uptrend" and ll:
            result["choch_bearish"] = True

    return result


def score_candle_for_direction(
    candle_pattern: str,
    candle_score: float,
    sr_signal: str,
    fvg: dict,
    order_blocks: dict,
    market_structure: dict,
    is_long: bool,
    fibonacci: dict = None,
) -> int:
    """
    Комплексная оценка свечного анализа + FVG + OB + MS + Фибоначчи.
    Полностью симметрична для лонгов и шортов.
    Возвращает очки -50..100.
    """
    score = 0

    # 1. СВЕЧНОЙ ПАТТЕРН (базовый сигнал)
    if is_long:
        if candle_pattern in ("rejection_low", "hammer", "inverted_hammer"):
            if sr_signal == "bounce_support":
                score += 30   # идеальное совпадение
            elif sr_signal == "bounce_resistance":
                score -= 20   # противоречие
            else:
                score += 18
        elif candle_pattern in ("bullish_engulfing", "bullish_marubozu"):
            if sr_signal == "bounce_resistance":
                score -= 12
            else:
                score += 15
        elif candle_pattern == "doji":
            score += 3
        elif candle_pattern in ("rejection_high", "shooting_star", "hanging_man"):
            score -= 20
        elif candle_pattern in ("bearish_engulfing", "bearish_marubozu"):
            score -= 25
    else:
        if candle_pattern in ("rejection_high", "shooting_star", "hanging_man"):
            if sr_signal == "bounce_resistance":
                score += 30   # идеальное совпадение
            elif sr_signal == "bounce_support":
                score -= 20   # противоречие
            else:
                score += 18
        elif candle_pattern in ("bearish_engulfing", "bearish_marubozu"):
            if sr_signal == "bounce_support":
                score -= 25  # сильное противоречие — медвежья свеча у поддержки
            elif sr_signal == "bounce_resistance":
                score -= 5   # нейтрально — продолжение падения
            else:
                score += 15
        elif candle_pattern == "doji":
            score += 3
        elif candle_pattern in ("rejection_low", "hammer", "inverted_hammer"):
            score -= 20
        elif candle_pattern in ("bullish_engulfing", "bullish_marubozu"):
            score -= 25

    # 2. FVG — симметрично
    if fvg:
        if is_long:
            if fvg.get("in_bullish_fvg"):
                score += 20
            elif fvg.get("bullish_fvg") and (fvg["bullish_fvg"].get("dist_pct") or 99) < 1.0:
                score += 10
            # Штраф если цена в медвежьем FVG при лонге
            if fvg.get("in_bearish_fvg"):
                score -= 10
        else:
            if fvg.get("in_bearish_fvg"):
                score += 20
            elif fvg.get("bearish_fvg") and (fvg["bearish_fvg"].get("dist_pct") or 99) < 1.0:
                score += 10
            # Штраф если цена в бычьем FVG при шорте
            if fvg.get("in_bullish_fvg"):
                score -= 10

    # 3. ORDER BLOCKS — симметрично
    if order_blocks:
        if is_long:
            if order_blocks.get("in_bullish_ob"):
                score += 15
            elif order_blocks.get("bullish_ob") and (order_blocks["bullish_ob"].get("dist_pct") or 99) < 1.0:
                score += 8
            if order_blocks.get("in_bearish_ob"):
                score -= 8
        else:
            if order_blocks.get("in_bearish_ob"):
                score += 15
            elif order_blocks.get("bearish_ob") and (order_blocks["bearish_ob"].get("dist_pct") or 99) < 1.0:
                score += 8
            if order_blocks.get("in_bullish_ob"):
                score -= 8

    # 4. MARKET STRUCTURE — симметрично
    if market_structure:
        struct = market_structure.get("structure", "ranging")
        if is_long:
            if market_structure.get("bos_bullish"):
                score += 12
            elif market_structure.get("choch_bullish"):
                score += 6
            elif struct == "uptrend":
                score += 8
            elif struct == "downtrend":
                score -= 8
        else:
            if market_structure.get("bos_bearish"):
                score += 12
            elif market_structure.get("choch_bearish"):
                score += 6
            elif struct == "downtrend":
                score += 8
            elif struct == "uptrend":
                score -= 8

    # 5. ФИБОНАЧЧИ — симметрично
    if fibonacci:
        if is_long:
            score += fibonacci.get("fib_score_long", 0)
        else:
            score += fibonacci.get("fib_score_short", 0)

    return max(-50, min(100, score))

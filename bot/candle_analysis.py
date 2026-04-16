"""
Candle Analysis Module — FVG, Order Blocks, Market Structure
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

        # Bullish OB: медвежья свеча + импульс вверх после
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

        # Bearish OB: бычья свеча + импульс вниз после
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
) -> int:
    """Комплексная оценка свечного анализа (-50..100)."""
    score = 0

    # 1. Свечной паттерн
    if is_long:
        if candle_pattern in ("rejection_low", "hammer", "inverted_hammer"):
            if sr_signal == "bounce_support":
                score += 60  # идеальное совпадение
            elif sr_signal == "bounce_resistance":
                score -= 30  # противоречие — свеча бычья но SR медвежий
            else:
                score += 35
        elif candle_pattern in ("bullish_engulfing", "bullish_marubozu"):
            if sr_signal == "bounce_resistance":
                score -= 20  # слабое противоречие
            else:
                score += 30
        elif candle_pattern == "doji":
            score += 5
        elif candle_pattern in ("rejection_high", "shooting_star", "hanging_man"):
            score -= 30
        elif candle_pattern in ("bearish_engulfing", "bearish_marubozu"):
            score -= 40
    else:
        if candle_pattern in ("rejection_high", "shooting_star", "hanging_man"):
            if sr_signal == "bounce_resistance":
                score += 60  # идеальное совпадение
            elif sr_signal == "bounce_support":
                score -= 30  # противоречие — свеча медвежья но SR бычий
            else:
                score += 35  # нет SR, но свеча медвежья
        elif candle_pattern in ("bearish_engulfing", "bearish_marubozu"):
            if sr_signal == "bounce_support":
                score -= 20  # слабое противоречие
            else:
                score += 30
        elif candle_pattern == "doji":
            score += 5
        elif candle_pattern in ("rejection_low", "hammer", "inverted_hammer"):
            score -= 30
        elif candle_pattern in ("bullish_engulfing", "bullish_marubozu"):
            score -= 40

    # 2. FVG
    if fvg:
        if is_long:
            if fvg.get("in_bullish_fvg"):
                score += 25
            elif fvg.get("bullish_fvg") and (fvg["bullish_fvg"].get("dist_pct") or 99) < 1.0:
                score += 12
        else:
            if fvg.get("in_bearish_fvg"):
                score += 25
            elif fvg.get("bearish_fvg") and (fvg["bearish_fvg"].get("dist_pct") or 99) < 1.0:
                score += 12

    # 3. Order Blocks
    if order_blocks:
        if is_long:
            if order_blocks.get("in_bullish_ob"):
                score += 20
            elif order_blocks.get("bullish_ob") and (order_blocks["bullish_ob"].get("dist_pct") or 99) < 1.0:
                score += 10
        else:
            if order_blocks.get("in_bearish_ob"):
                score += 20
            elif order_blocks.get("bearish_ob") and (order_blocks["bearish_ob"].get("dist_pct") or 99) < 1.0:
                score += 10

    # 4. Market Structure
    if market_structure:
        struct = market_structure.get("structure", "ranging")
        if is_long:
            if market_structure.get("bos_bullish"):
                score += 15
            elif market_structure.get("choch_bullish"):
                score += 8
            elif struct == "uptrend":
                score += 10
            elif struct == "downtrend":
                score -= 10
        else:
            if market_structure.get("bos_bearish"):
                score += 15
            elif market_structure.get("choch_bearish"):
                score += 8
            elif struct == "downtrend":
                score += 10
            elif struct == "uptrend":
                score -= 10

    return max(-50, min(100, score))

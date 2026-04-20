from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
import db
import os
import json
import csv
import io
from typing import Optional, Any

app = FastAPI(title="CryptoBot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE = os.path.dirname(__file__)


def _snapshot_dict(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


@app.get("/")
async def dashboard():
    return FileResponse(os.path.join(BASE, 'dashboard.html'))

@app.get("/manifest.json")
async def manifest():
    return FileResponse(os.path.join(BASE, 'manifest.json'), media_type='application/manifest+json')

@app.get("/sw.js")
async def service_worker():
    return FileResponse(os.path.join(BASE, 'sw.js'), media_type='application/javascript')

@app.get("/icon-192.png")
async def icon192():
    return FileResponse(os.path.join(BASE, 'icon-192.png'), media_type='image/png')

@app.get("/icon-512.png")
async def icon512():
    return FileResponse(os.path.join(BASE, 'icon-512.png'), media_type='image/png')

@app.get("/api/status")
async def get_status():
    account = await db.fetchrow("SELECT * FROM crypto_demo_accounts WHERE is_active=true LIMIT 1")
    open_trades = await db.fetchval("SELECT COUNT(*) FROM crypto_demo_trades WHERE status='open'")
    closed_trades = await db.fetchval("SELECT COUNT(*) FROM crypto_demo_trades WHERE status='closed'")
    fg = await db.fetchrow("SELECT value, label FROM crypto_fear_greed WHERE id='latest'")
    params = await db.fetchrow("SELECT strategy_mode, kill_switch_active FROM crypto_strategy_params WHERE id='current'")
    return {
        "balance": float(account["current_balance"]) if account else 0,
        "initial_balance": float(account["initial_balance"]) if account else 0,
        "pnl": float(account["current_balance"] - account["initial_balance"]) if account else 0,
        "open_trades": open_trades,
        "closed_trades": closed_trades,
        "fear_greed": {"value": float(fg["value"]), "label": fg["label"]} if fg else None,
        "strategy_mode": params["strategy_mode"] if params else "unknown",
        "kill_switch": params["kill_switch_active"] if params else False,
    }

@app.get("/api/positions")
async def get_positions():
    rows = await db.fetch("""
        SELECT t.*, p.price as current_price
        FROM crypto_demo_trades t
        LEFT JOIN LATERAL (
            SELECT price FROM crypto_prices_bybit WHERE symbol=t.symbol ORDER BY ts DESC LIMIT 1
        ) p ON true
        WHERE t.status='open' ORDER BY t.opened_at DESC
    """)
    result = []
    for r in rows:
        entry = float(r["entry_price"])
        current = float(r["current_price"]) if r["current_price"] else entry
        size = float(r["amount_usdt"])
        crypto = float(r["amount_crypto"])
        direction = r["trade_type"]
        pnl = (current - entry) * crypto if direction == "long" else (entry - current) * crypto
        sl_price = float(r["sl_price"]) if r.get("sl_price") is not None else None
        snap = _snapshot_dict(r.get("features_snapshot"))
        market_mode = snap.get("market_mode") or ""
        sr_signal = snap.get("sr_signal") or ""
        dist_sl_pct = None
        if sl_price and sl_price > 0 and current > 0:
            if direction == "long":
                dist_sl_pct = round((current - sl_price) / current * 100, 2)
            else:
                dist_sl_pct = round((sl_price - current) / current * 100, 2)
        result.append({
            "id": str(r["id"]),
            "symbol": r["symbol"],
            "direction": direction,
            "entry_price": entry,
            "current_price": current,
            "size": size,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / size * 100, 2),
            "opened_at": r["opened_at"].isoformat(),
            "sl_price": sl_price,
            "setup_type": r.get("setup_type") or "",
            "market_mode": market_mode,
            "sr_signal": sr_signal,
            "dist_to_sl_pct": dist_sl_pct,
        })
    return result

_TRADE_SELECT = """
            SELECT
                t.id, t.symbol, t.trade_type, t.entry_price, t.exit_price,
                t.amount_usdt, t.close_reason, t.opened_at, t.closed_at,
                t.setup_type, t.features_snapshot,
                COALESCE(t.pnl_usdt, 0) +
                COALESCE((
                    SELECT SUM(h.pnl_usdt)
                    FROM crypto_demo_trades h
                    WHERE h.symbol = t.symbol
                    AND h.opened_at = t.opened_at
                    AND h.close_reason LIKE '%tp1_partial%'
                    AND h.status = 'closed'
                    AND h.id != t.id
                ), 0) as total_pnl
            FROM crypto_demo_trades t
            WHERE t.status='closed'
            AND t.pnl_usdt IS NOT NULL
            AND t.close_reason NOT LIKE '%tp1_partial%'
"""

@app.get("/api/trades")
async def get_trades(limit: int = 50, days: Optional[int] = None):
    if days is not None and days > 0:
        rows = await db.fetch(
            _TRADE_SELECT
            + """
            AND t.closed_at >= now() - $1::interval
            ORDER BY t.closed_at DESC LIMIT $2
            """,
            f"{int(days)} days",
            limit,
        )
    else:
        rows = await db.fetch(
            _TRADE_SELECT + " ORDER BY t.closed_at DESC LIMIT $1",
            limit,
        )

    result = []
    for r in rows:
        total_pnl = float(r["total_pnl"] or 0)
        size = float(r["amount_usdt"])
        close_reason = r["close_reason"] or ""
        if total_pnl > float(r.get("pnl_usdt") or 0) * 1.1:
            close_reason = "tp1_partial+" + close_reason
        snap = _snapshot_dict(r.get("features_snapshot"))
        opened = r["opened_at"]
        closed = r["closed_at"]
        hold_h = None
        if opened and closed:
            hold_h = round((closed - opened).total_seconds() / 3600, 1)
        result.append({
            "id": str(r["id"]),
            "symbol": r["symbol"],
            "direction": r["trade_type"],
            "entry_price": float(r["entry_price"]),
            "exit_price": float(r["exit_price"]) if r["exit_price"] else None,
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl / size * 100, 2),
            "close_reason": close_reason,
            "opened_at": opened.isoformat() if opened else None,
            "closed_at": closed.isoformat() if closed else None,
            "hold_hours": hold_h,
            "setup_type": r.get("setup_type") or "",
            "market_mode": snap.get("market_mode") or "",
            "sr_signal": snap.get("sr_signal") or "",
        })
    return result

@app.get("/api/trades_export.csv")
async def get_trades_export(days: Optional[int] = None):
    """CSV экспорт закрытых сделок."""
    trades = await get_trades(limit=5000, days=days)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "id", "symbol", "direction", "entry_price", "exit_price", "pnl", "pnl_pct",
        "setup_type", "market_mode", "sr_signal", "hold_hours", "close_reason",
        "opened_at", "closed_at",
    ])
    for t in trades:
        w.writerow([
            t["id"], t["symbol"], t["direction"], t["entry_price"], t.get("exit_price"),
            t["pnl"], t["pnl_pct"], t.get("setup_type"), t.get("market_mode"),
            t.get("sr_signal"), t.get("hold_hours"), t["close_reason"],
            t["opened_at"], t["closed_at"],
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="crypto_demo_trades.csv"'},
    )

@app.get("/api/balance_history")
async def get_balance_history(days: Optional[int] = None):
    account = await db.fetchrow("SELECT initial_balance FROM crypto_demo_accounts WHERE is_active=true LIMIT 1")
    initial = float(account["initial_balance"]) if account else 0.0

    if days is not None and days > 0:
        pnl_before = await db.fetchval(
            """SELECT COALESCE(SUM(pnl_usdt), 0) FROM crypto_demo_trades
               WHERE status='closed' AND pnl_usdt IS NOT NULL
               AND closed_at < now() - $1::interval""",
            f"{int(days)} days",
        )
        start_cum = initial + float(pnl_before or 0)
        rows = await db.fetch(
            """SELECT DATE_TRUNC('hour', closed_at) as hour,
                      SUM(pnl_usdt) as hourly_pnl
               FROM crypto_demo_trades
               WHERE status='closed' AND pnl_usdt IS NOT NULL
               AND closed_at >= now() - $1::interval
               GROUP BY hour ORDER BY hour""",
            f"{int(days)} days",
        )
    else:
        start_cum = initial
        rows = await db.fetch("""
            SELECT DATE_TRUNC('hour', closed_at) as hour,
                   SUM(pnl_usdt) as hourly_pnl
            FROM crypto_demo_trades
            WHERE status='closed' AND pnl_usdt IS NOT NULL
            GROUP BY hour ORDER BY hour
        """)

    cumulative = start_cum
    result = []
    for r in rows:
        cumulative += float(r["hourly_pnl"])
        result.append({"time": r["hour"].isoformat(), "balance": round(cumulative, 2)})
    return result

@app.get("/api/daily_pnl")
async def get_daily_pnl(days: int = 90):
    """Суммарный PnL по календарным дням (UTC)."""
    d = max(1, min(int(days), 365))
    rows = await db.fetch(
        """SELECT DATE_TRUNC('day', closed_at AT TIME ZONE 'UTC')::date as day,
                  SUM(pnl_usdt) as day_pnl
           FROM crypto_demo_trades
           WHERE status='closed' AND pnl_usdt IS NOT NULL
           AND closed_at >= now() - $1::interval
           GROUP BY 1 ORDER BY 1""",
        f"{d} days",
    )
    return [{"day": r["day"].isoformat(), "pnl": round(float(r["day_pnl"]), 2)} for r in rows]

@app.get("/api/stats")
async def get_stats(days: Optional[int] = None):
    where_extra = ""
    args: list = []
    if days is not None and days > 0:
        where_extra = "AND closed_at >= $1::interval"
        args.append(f"{int(days)} days")

    total = await db.fetchval(
        f"SELECT COUNT(*) FROM crypto_demo_trades WHERE status='closed' {where_extra}",
        *args,
    )
    if not total:
        return {
            "total": 0,
            "days": days,
            "by_reason": [],
            "by_reason_bucket": [],
        }

    wins = await db.fetchval(
        f"SELECT COUNT(*) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt > 0 {where_extra}",
        *args,
    )
    total_pnl = await db.fetchval(
        f"SELECT SUM(pnl_usdt) FROM crypto_demo_trades WHERE status='closed' {where_extra}",
        *args,
    )
    sum_wins = await db.fetchval(
        f"SELECT COALESCE(SUM(pnl_usdt), 0) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt > 0 {where_extra}",
        *args,
    )
    sum_losses = await db.fetchval(
        f"SELECT COALESCE(SUM(pnl_usdt), 0) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt < 0 {where_extra}",
        *args,
    )
    avg_win = await db.fetchval(
        f"SELECT AVG(pnl_usdt) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt > 0 {where_extra}",
        *args,
    )
    avg_loss = await db.fetchval(
        f"SELECT AVG(pnl_usdt) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt < 0 {where_extra}",
        *args,
    )

    loss_sum_abs = abs(float(sum_losses or 0))
    profit_factor = None
    if loss_sum_abs > 1e-9:
        profit_factor = round(float(sum_wins or 0) / loss_sum_abs, 3)
    elif float(sum_wins or 0) > 0:
        pass

    expectancy = round(float(total_pnl or 0) / total, 2) if total else 0.0

    account = await db.fetchrow("SELECT initial_balance FROM crypto_demo_accounts WHERE is_active=true LIMIT 1")
    initial = float(account["initial_balance"]) if account else 0.0

    if args:
        pnl_before_win = await db.fetchval(
            """SELECT COALESCE(SUM(pnl_usdt), 0) FROM crypto_demo_trades
               WHERE status='closed' AND pnl_usdt IS NOT NULL
               AND closed_at < now() - $1::interval""",
            args[0],
        )
        start_equity = initial + float(pnl_before_win or 0)
    else:
        start_equity = initial

    max_drawdown = 0.0
    dd_rows = await db.fetch(
        f"""
        SELECT closed_at, pnl_usdt
        FROM crypto_demo_trades
        WHERE status='closed' AND pnl_usdt IS NOT NULL AND closed_at IS NOT NULL
        {where_extra}
        ORDER BY closed_at
        """,
        *args,
    )
    cum = start_equity
    peak = start_equity
    for row in dd_rows:
        cum += float(row["pnl_usdt"] or 0)
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_drawdown:
            max_drawdown = dd

    by_reason = await db.fetch(
        f"""
        SELECT close_reason, COUNT(*) as cnt, SUM(pnl_usdt) as total_pnl
        FROM crypto_demo_trades WHERE status='closed' {where_extra}
        GROUP BY close_reason ORDER BY total_pnl DESC
        """,
        *args,
    )

    by_bucket = await db.fetch(
        f"""
        SELECT
          CASE
            WHEN close_reason IS NULL THEN '(null)'
            WHEN close_reason LIKE 'opposite_forecast%%' THEN 'opposite_forecast*'
            WHEN close_reason LIKE 'weak_forecast%%' THEN 'weak_forecast*'
            WHEN close_reason LIKE '%%forecast_decay%%' THEN 'forecast_decay*'
            ELSE close_reason
          END AS bucket,
          COUNT(*) AS cnt,
          SUM(pnl_usdt) AS total_pnl
        FROM crypto_demo_trades
        WHERE status='closed' {where_extra}
        GROUP BY 1
        ORDER BY total_pnl ASC
        """,
        *args,
    )

    return {
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": round(wins / total * 100, 1),
        "total_pnl": round(float(total_pnl or 0), 2),
        "avg_win": round(float(avg_win or 0), 2),
        "avg_loss": round(float(avg_loss or 0), 2),
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "max_drawdown": round(max_drawdown, 2),
        "days": days,
        "by_reason": [
            {"reason": r["close_reason"], "count": r["cnt"], "pnl": round(float(r["total_pnl"]), 2)}
            for r in by_reason
        ],
        "by_reason_bucket": [
            {"reason": r["bucket"], "count": r["cnt"], "pnl": round(float(r["total_pnl"]), 2)}
            for r in by_bucket
        ],
    }

from fastapi import WebSocket, WebSocketDisconnect
import asyncio

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            try:
                account = await db.fetchrow("SELECT current_balance, initial_balance FROM crypto_demo_accounts WHERE is_active=true LIMIT 1")
                open_trades = await db.fetchval("SELECT COUNT(*) FROM crypto_demo_trades WHERE status='open'")
                fg = await db.fetchrow("SELECT value, label FROM crypto_fear_greed WHERE id='latest'")

                await websocket.send_json({
                    "type": "update",
                    "balance": float(account["current_balance"]) if account else 0,
                    "pnl": float(account["current_balance"] - account["initial_balance"]) if account else 0,
                    "open_trades": open_trades,
                    "fear_greed": {"value": float(fg["value"]), "label": fg["label"]} if fg else None,
                })
            except Exception:
                pass
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass

@app.get("/api/test_bybit")
async def test_bybit():
    import aiohttp as _aiohttp
    results = {}
    async with _aiohttp.ClientSession() as s:
        for path, params in [
            ("/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"}),
            ("/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT", "interval": "60", "limit": "5"}),
            ("/v5/market/orderbook", {"category": "linear", "symbol": "BTCUSDT", "limit": "5"}),
            ("/v5/market/time", {}),
        ]:
            try:
                async with s.get("https://api.bybit.com" + path, params=params,
                                 timeout=_aiohttp.ClientTimeout(total=5)) as r:
                    ct = r.headers.get("content-type", "")
                    if "json" in ct:
                        data = await r.json()
                        results[path] = {"status": r.status, "retCode": data.get("retCode"), "ok": True}
                    else:
                        text = await r.text()
                        results[path] = {"status": r.status, "content_type": ct, "ok": False, "body": text[:200]}
            except Exception as e:
                results[path] = {"ok": False, "error": str(e)}
    return results




@app.get("/api/candles")
async def get_candles(symbol: str = "BTC", interval: str = "60", limit: int = 200):
    """Свечи для графика — проксируем из Bybit."""
    import aiohttp
    from config import bybit_symbol
    try:
        bs = bybit_symbol(symbol)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.bybit.com/v5/market/kline",
                params={"category": "linear", "symbol": bs, "interval": interval, "limit": limit},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                if data.get("retCode") != 0:
                    return []
                candles = [
                    {
                        "time": int(r[0]) // 1000,
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[5]),
                    }
                    for r in reversed(data["result"]["list"])
                ]
                return candles
    except Exception:
        return []


@app.get("/api/signals")
async def get_signals():
    """Монеты с активными SR сигналами прямо сейчас."""
    rows = await db.fetch("""
        SELECT DISTINCT ON (symbol)
            symbol, sr_signal, sr_strength,
            ROUND(distance_to_support_pct::numeric, 2) as dist_sup,
            ROUND(distance_to_resistance_pct::numeric, 2) as dist_res,
            candlestick_pattern,
            ROUND(rsi_14::numeric, 1) as rsi,
            market_mode,
            ROUND(r_1h::numeric, 3) as r_1h,
            volume_bucket
        FROM crypto_features_hourly
        WHERE ts > now() - interval '10 minutes'
        AND sr_signal != 'neutral'
        ORDER BY symbol, ts DESC
    """)
    return [dict(r) for r in rows]


@app.get("/api/market_context")
async def get_market_context():
    """Текущий контекст рынка."""
    row = await db.fetchrow("""
        SELECT features_snapshot FROM crypto_market_global WHERE id='latest'
    """)
    if row and row["features_snapshot"]:
        ctx = json.loads(row["features_snapshot"]) if isinstance(row["features_snapshot"], str) else row["features_snapshot"]
        return ctx
    return {}


@app.get("/api/debug_features")
async def debug_features(symbol: str = "BTC"):
    """Debug endpoint — проверяем candle scores."""
    row = await db.fetchrow("""
        SELECT candlestick_pattern, candle_score_long, candle_score_short,
               sr_signal, ms_structure, in_bullish_fvg, in_bearish_fvg
        FROM crypto_features_hourly
        WHERE symbol=$1
        ORDER BY ts DESC LIMIT 1
    """, symbol)
    if not row:
        return {"error": "no data"}
    return dict(row)

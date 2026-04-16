from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
import db
import os

app = FastAPI(title="CryptoBot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE = os.path.dirname(__file__)

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
        pnl = (current - entry) * crypto if r["trade_type"] == "long" else (entry - current) * crypto
        result.append({
            "id": str(r["id"]),
            "symbol": r["symbol"],
            "direction": r["trade_type"],
            "entry_price": entry,
            "current_price": current,
            "size": size,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / size * 100, 2),
            "opened_at": r["opened_at"].isoformat(),
        })
    return result

@app.get("/api/trades")
async def get_trades(limit: int = 50):
    # Получаем все закрытые сделки включая частичные закрытия
    rows = await db.fetch("""
        SELECT
            t.id, t.symbol, t.trade_type, t.entry_price, t.exit_price,
            t.amount_usdt, t.close_reason, t.opened_at, t.closed_at,
            -- Суммируем PnL включая частичные закрытия (tp1_partial)
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
        ORDER BY t.closed_at DESC LIMIT $1
    """, limit)

    result = []
    for r in rows:
        total_pnl = float(r["total_pnl"] or 0)
        size = float(r["amount_usdt"])
        close_reason = r["close_reason"] or ""
        # Показываем что была частичная фиксация
        if total_pnl > float(r.get("pnl_usdt") or 0) * 1.1:
            close_reason = "tp1_partial+" + close_reason
        result.append({
            "id": str(r["id"]),
            "symbol": r["symbol"],
            "direction": r["trade_type"],
            "entry_price": float(r["entry_price"]),
            "exit_price": float(r["exit_price"]) if r["exit_price"] else None,
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl / size * 100, 2),
            "close_reason": close_reason,
            "opened_at": r["opened_at"].isoformat(),
            "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
        })
    return result

@app.get("/api/balance_history")
async def get_balance_history():
    rows = await db.fetch("""
        SELECT DATE_TRUNC('hour', closed_at) as hour,
               SUM(pnl_usdt) as hourly_pnl
        FROM crypto_demo_trades
        WHERE status='closed' AND pnl_usdt IS NOT NULL
        GROUP BY hour ORDER BY hour
    """)
    cumulative = 10000000.0
    result = []
    for r in rows:
        cumulative += float(r["hourly_pnl"])
        result.append({"time": r["hour"].isoformat(), "balance": round(cumulative, 2)})
    return result

@app.get("/api/stats")
async def get_stats():
    total = await db.fetchval("SELECT COUNT(*) FROM crypto_demo_trades WHERE status='closed'")
    if not total:
        return {"total": 0}
    wins = await db.fetchval("SELECT COUNT(*) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt > 0")
    total_pnl = await db.fetchval("SELECT SUM(pnl_usdt) FROM crypto_demo_trades WHERE status='closed'")
    avg_win = await db.fetchval("SELECT AVG(pnl_usdt) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt > 0")
    avg_loss = await db.fetchval("SELECT AVG(pnl_usdt) FROM crypto_demo_trades WHERE status='closed' AND pnl_usdt < 0")
    by_reason = await db.fetch("""
        SELECT close_reason, COUNT(*) as cnt, SUM(pnl_usdt) as total_pnl
        FROM crypto_demo_trades WHERE status='closed'
        GROUP BY close_reason ORDER BY total_pnl DESC
    """)
    return {
        "total": total, "wins": wins, "losses": total - wins,
        "win_rate": round(wins / total * 100, 1),
        "total_pnl": round(float(total_pnl or 0), 2),
        "avg_win": round(float(avg_win or 0), 2),
        "avg_loss": round(float(avg_loss or 0), 2),
        "by_reason": [{"reason": r["close_reason"], "count": r["cnt"], "pnl": round(float(r["total_pnl"]), 2)} for r in by_reason],
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
            except Exception as e:
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
    except Exception as e:
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
        import json
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

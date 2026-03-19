from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import db

app = FastAPI(title="CryptoBot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    rows = await db.fetch("""
        SELECT * FROM crypto_demo_trades
        WHERE status='closed' AND pnl_usdt IS NOT NULL
        ORDER BY closed_at DESC LIMIT $1
    """, limit)
    return [{
        "id": str(r["id"]),
        "symbol": r["symbol"],
        "direction": r["trade_type"],
        "entry_price": float(r["entry_price"]),
        "exit_price": float(r["exit_price"]) if r["exit_price"] else None,
        "pnl": float(r["pnl_usdt"]),
        "pnl_pct": round(float(r["pnl_usdt"]) / float(r["amount_usdt"]) * 100, 2),
        "close_reason": r["close_reason"],
        "opened_at": r["opened_at"].isoformat(),
        "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
    } for r in rows]

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
        result.append({
            "time": r["hour"].isoformat(),
            "balance": round(cumulative, 2),
        })
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
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": round(wins / total * 100, 1),
        "total_pnl": round(float(total_pnl or 0), 2),
        "avg_win": round(float(avg_win or 0), 2),
        "avg_loss": round(float(avg_loss or 0), 2),
        "by_reason": [{"reason": r["close_reason"], "count": r["cnt"], "pnl": round(float(r["total_pnl"]), 2)} for r in by_reason],
    }

from fastapi.responses import FileResponse
import os

@app.get("/")
async def dashboard():
    path = os.path.join(os.path.dirname(__file__), '..', 'dashboard.html')
    return FileResponse(os.path.abspath(path))

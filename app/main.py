from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from app.kalshi_trader import get_trader

app = FastAPI()

# ─── HEALTH CHECK (Railway needs this) ───
@app.get("/health")
async def health():
    return {"status": "ok", "service": "sixfilter-kalshi"}

# ─── KALSHI ENDPOINTS ───

@app.get("/kalshi/scan")
async def kalshi_scan():
    """Run SixFilter cycle on all Kalshi markets."""
    try:
        trader = get_trader()
        signals = trader.run_cycle()
        return {"signals_found": len(signals), "signals": signals}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/kalshi/balance")
async def kalshi_balance():
    """Check Kalshi account balance."""
    try:
        trader = get_trader()
        balance = trader.client.get_balance()
        return {
            "balance_cents": balance.balance,
            "balance_dollars": round(balance.balance / 100, 2),
            "withdrawable_cents": balance.withdrawable_balance
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/kalshi/markets")
async def kalshi_markets():
    """List open economics/finance markets."""
    try:
        trader = get_trader()
        markets = trader.scan_markets()
        return {"markets_count": len(markets), "markets": markets[:30]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ManualTradeRequest(BaseModel):
    ticker: str
    side: str
    price: int
    count: int

@app.post("/kalshi/trade")
async def kalshi_manual_trade(req: ManualTradeRequest):
    """Manual trade execution."""
    try:
        trader = get_trader()
        signal = {
            'ticker': req.ticker,
            'side': req.side,
            'price': req.price,
            'count': req.count
        }
        order_id = trader.execute_trade(signal)
        return {
            "order_id": order_id,
            "status": "executed" if order_id else "failed",
            "ticker": req.ticker,
            "side": req.side,
            "price": req.price,
            "count": req.count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/kalshi/config")
async def kalshi_config():
    """Show current SixFilter config."""
    try:
        trader = get_trader()
        config = trader.config
        return {
            "bankroll": config.BANKROLL,
            "min_edge_pct": config.MIN_EDGE_PCT,
            "kelly_fraction": config.KELLY_FRACTION,
            "max_position_pct": config.MAX_POSITION_PCT,
            "min_ev_cents": config.MIN_EV_CENTS,
            "max_spread_cents": config.MAX_SPREAD_CENTS,
            "target_categories": config.TARGET_CATEGORIES
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

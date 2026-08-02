import os
import json
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="SixFilter Kalshi Trader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

kalshi = None
kalshi_config_data = {
    "env": os.getenv("KALSHI_ENV", "demo"),
    "key_id_set": bool(os.getenv("KALSHI_KEY_ID")),
    "key_loaded": False,
    "key_source": "none",
    "base_url": "https://demo-api.kalshi.com/trade-api/v2",
    "error": None
}

try:
    from kalshi_trader import KalshiClient
    kalshi = KalshiClient()
    kalshi_config_data = kalshi.get_config()
except Exception as e:
    kalshi_config_data["error"] = str(e)
    kalshi = None

class OrderRequest(BaseModel):
    ticker: str
    side: str
    count: str
    price: str
    client_order_id: str = None

@app.get("/health")
def health():
    return {
        "status": "ok",
        "kalshi": kalshi_config_data,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/kalshi/config")
def kalshi_config():
    if kalshi is None:
        return kalshi_config_data
    return kalshi.get_config()

@app.get("/kalshi/balance")
def kalshi_balance():
    if kalshi is None or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.get_balance()

@app.get("/kalshi/markets")
def kalshi_markets(limit: int = 100):
    if kalshi is None or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.get_markets(limit)

@app.post("/kalshi/order")
def kalshi_order(order: OrderRequest):
    if kalshi is None or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.place_order(
        ticker=order.ticker,
        side=order.side,
        count=order.count,
        price=order.price,
        client_order_id=order.client_order_id
    )

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    if os.path.exists("dashboard.html"):
        with open("dashboard.html", "r") as f:
            return f.read()
    return "<html><body><h1>Dashboard not found</h1></body></html>"

@app.get("/")
def root():
    return {"message": "SixFilter Kalshi Trader API", "docs": "/docs"}

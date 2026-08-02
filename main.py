import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="SixFilter Kalshi Trader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Safe Kalshi init — never crashes the app
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
    kalshi = None
    kalshi_config_data["error"] = str(e)

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

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    if os.path.exists("dashboard.html"):
        with open("dashboard.html", "r") as f:
            return f.read()
    return "<html><body><h1>Dashboard not found</h1></body></html>"

@app.get("/")
def root():
    return {"message": "SixFilter Kalshi Trader API", "docs": "/docs"}

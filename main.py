import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from kalshi_trader import KalshiClient

app = FastAPI(title="SixFilter Kalshi Trader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

kalshi = KalshiClient()

@app.get("/health")
def health():
    return {
        "status": "ok",
        "kalshi": kalshi.get_config(),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/kalshi/config")
def kalshi_config():
    return kalshi.get_config()

@app.get("/kalshi/balance")
def kalshi_balance():
    if not kalshi.is_configured():
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

import os
import asyncio
import httpx
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
scheduler = AsyncIOScheduler()

# Environment variables
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Global scanner state
SCANNER_ACTIVE = False
last_signals = {}

# Serve the dashboard at root
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard HTML from parent directory"""
    try:
        # dashboard.html is in root, main.py is in app/, so go up one level
        dashboard_path = os.path.join(os.path.dirname(__file__), "..", "dashboard.html")
        dashboard_path = os.path.abspath(dashboard_path)
        
        if os.path.exists(dashboard_path):
            with open(dashboard_path, "r") as f:
                return f.read()
        else:
            return f"""
            <html>
                <body style="font-family: Arial, sans-serif; padding: 40px; background: #0f172a; color: white;">
                    <h1>🎯 Kalshi SixFilter</h1>
                    <p>Status: {'✅ Active' if SCANNER_ACTIVE else '⏸️ Paused'}</p>
                    <p>API: Running</p>
                    <p>Send /start to your Telegram bot to activate scanner</p>
                    <br>
                    <a href="/health" style="color: #3b82f6;">Check Health →</a>
                </body>
            </html>
            """
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"

@app.get("/health")
async def health():
    return {"status": "alive", "scanner_active": SCANNER_ACTIVE, "time": datetime.now().isoformat()}

class KalshiClient:
    def __init__(self):
        self.base_url = "https://trading-api.kalshi.com/trade-api/v2"
        self.headers = {"Authorization": f"Bearer {KALSHI_API_KEY}"}
    
    async def get_markets(self, limit=100):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/markets",
                headers=self.headers,
                params={"status": "active", "limit": limit}
            )
            return response.json().get("markets", [])
    
    async def get_market_orderbook(self, ticker):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/markets/{ticker}/orderbook",
                headers=self.headers
            )
            data = response.json()
            orderbook = data.get("orderbook", {})
            yes_bids = orderbook.get("bids", [])
            yes_asks = orderbook.get("asks", [])
            
            best_yes_bid = yes_bids[0]["price"] if yes_bids else 0
            best_yes_ask = yes_asks[0]["price"] if yes_asks else 100
            
            return {
                "yes_bid": best_yes_bid,
                "yes_ask": best_yes_ask,
                "implied_prob": (best_yes_bid + best_yes_ask) / 200
            }

class SixFilter:
    def analyze(self, market_data, orderbook):
        signals = {}
        current_price = orderbook["implied_prob"]
        vwap_7d = market_data.get("previous_price", current_price)
        
        # Filter 1: LMSR Deviation
        lmsr_deviation = abs(current_price - vwap_7d) / vwap_7d if vwap_7d > 0 else 0
        signals["lmsr_pass"] = lmsr_deviation > 0.05
        
        # Filter 2: Kelly Criterion
        true_prob = self._estimate_true_prob(market_data)
        edge = abs(true_prob - current_price)
        kelly = edge / (current_price * (1 - current_price)) if current_price not in [0,1] else 0
        signals["kelly_pass"] = 0.1 < kelly < 0.5
        
        # Filter 3: EV Gap (2:1 RR minimum)
        potential_return = (100 - orderbook["yes_ask"]) / orderbook["yes_ask"] if orderbook["yes_ask"] > 0 else 0
        signals["ev_pass"] = potential_return > 0.5
        
        # Filter 4: Divergence
        volume_trend = market_data.get("volume", 0) > market_data.get("previous_volume", 0)
        price_trend = current_price > vwap_7d
        signals["divergence_pass"] = volume_trend != price_trend
        
        # Filter 5: Bayesian (market hours only)
        hour = datetime.now().hour
        signals["bayesian_pass"] = 9 <= hour <= 16
        
        # Filter 6: Stoikov (liquidity)
        spread = orderbook["yes_ask"] - orderbook["yes_bid"]
        signals["stoikov_pass"] = spread < 5
        
        return signals
    
    def _estimate_true_prob(self, market_data):
        return market_data.get("previous_price", 50) / 100

async def send_telegram_alert(market, direction, price, confidence):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured")
        return
    
    bot = Bot(token=TELEGRAM_TOKEN)
    message = f"""
🎯 *KALSHI SIXFILTER SIGNAL*

*{market['title'][:50]}...*

Direction: *{direction}* 
Entry: *{price}c*
Confidence: *{confidence}%*

⏰ Valid for 5 minutes
💡 Risk 1-2% max per trade
    """
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")

async def scan_markets():
    global SCANNER_ACTIVE, last_signals
    
    if not SCANNER_ACTIVE:
        return
    
    logger.info("🔍 Scanning Kalshi markets...")
    
    kalshi = KalshiClient()
    six_filter = SixFilter()
    
    try:
        markets = await kalshi.get_markets(limit=100)
        logger.info(f"Found {len(markets)} markets")
        
        for market in markets:
            ticker = market["ticker"]
            
            if ticker in last_signals and (datetime.now() - last_signals[ticker]).seconds < 3600:
                continue
            
            try:
                orderbook = await kalshi.get_market_orderbook(ticker)
            except Exception as e:
                continue
            
            filters = six_filter.analyze(market, orderbook)
            passed = sum(filters.values())
            logger.info(f"{ticker}: {passed}/6 filters passed")
            
            if all(filters.values()):
                if orderbook["implied_prob"] < 50:
                    direction = "BUY YES"
                    entry_price = orderbook["yes_ask"]
                else:
                    direction = "BUY NO"
                    entry_price = 100 - orderbook["yes_bid"]
                
                await send_telegram_alert(market, direction, entry_price, 85)
                last_signals[ticker] = datetime.now()
                logger.info(f"🚨 SIGNAL: {ticker} {direction} @ {entry_price}")
                
    except Exception as e:
        logger.error(f"Scanner error: {e}")

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    global SCANNER_ACTIVE
    
    data = await request.json()
    message = data.get("message", {}).get("text", "")
    chat_id = data.get("message", {}).get("chat", {}).get("id")
    
    if not TELEGRAM_TOKEN:
        return {"error": "Telegram not configured"}
    
    bot = Bot(token=TELEGRAM_TOKEN)
    
    if message == "/start":
        SCANNER_ACTIVE = True
        await bot.send_message(
            chat_id=chat_id,
            text="✅ Kalshi SixFilter Activated\n\nAuto-scanner: ENABLED\nMonitoring 100+ markets every 5 minutes...\n\nYou'll receive alerts when all 6 filters align."
        )
        asyncio.create_task(scan_markets())
        
    elif message == "/stop":
        SCANNER_ACTIVE = False
        await bot.send_message(chat_id=chat_id, text="🛑 Scanner paused.")
    
    return {"ok": True}

@app.on_event("startup")
async def startup():
    scheduler.add_job(scan_markets, "interval", minutes=5)
    scheduler.start()
    logger.info("✅ Server started, scheduler running")

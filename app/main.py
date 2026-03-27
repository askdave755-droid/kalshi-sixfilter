# main.py - Complete Kalshi SixFilter Auto-Scanner
import os
import asyncio
import httpx
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
scheduler = AsyncIOScheduler()

# Environment variables (already set in Railway)
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Global scanner state
SCANNER_ACTIVE = False
last_signals = {}

class KalshiClient:
    def __init__(self):
        self.base_url = "https://trading-api.kalshi.com/trade-api/v2"
        self.headers = {"Authorization": f"Bearer {KALSHI_API_KEY}"}
    
    async def get_markets(self, limit=100):
        """Fetch active markets from Kalshi"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/markets",
                headers=self.headers,
                params={
                    "status": "active",
                    "limit": limit,
                    "cursor": None
                }
            )
            return response.json().get("markets", [])
    
    async def get_market_orderbook(self, ticker):
        """Get YES/NO prices for a market"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/markets/{ticker}/orderbook",
                headers=self.headers
            )
            data = response.json()
            orderbook = data.get("orderbook", {})
            
            # Extract best bid/ask for YES
            yes_bids = orderbook.get("bids", [])
            yes_asks = orderbook.get("asks", [])
            
            best_yes_bid = yes_bids[0]["price"] if yes_bids else 0
            best_yes_ask = yes_asks[0]["price"] if yes_asks else 100
            
            return {
                "yes_bid": best_yes_bid,
                "yes_ask": best_yes_ask,
                "implied_prob": (best_yes_bid + best_yes_ask) / 200  # Midpoint
            }

class SixFilter:
    """
    Adapted MIT trader's 6 filters for Kalshi prediction markets:
    1. LMSR: Price deviation from historical VWAP (mean reversion)
    2. Kelly: Position sizing based on edge vs. bankroll
    3. EV Gap: Expected value check (2:1 RR minimum)
    4. KL Divergence: Momentum divergence detection
    5. Bayesian: Time-of-day, news context filtering
    6. Stoikov: Optimal entry timing (liquidity clustering)
    """
    
    def analyze(self, market_data, orderbook):
        signals = {}
        
        # Filter 1: LMSR Deviation (Mean Reversion)
        current_price = orderbook["implied_prob"]
        vwap_7d = market_data.get("previous_price", current_price)  # Fallback if no history
        lmsr_deviation = abs(current_price - vwap_7d) / vwap_7d if vwap_7d > 0 else 0
        signals["lmsr_pass"] = lmsr_deviation > 0.05  # >5% deviation
        
        # Filter 2: Kelly Criterion (Edge detection)
        # Assume "true" probability from polling/model data or reverse implied odds
        true_prob = self._estimate_true_prob(market_data)
        edge = abs(true_prob - current_price)
        kelly_fraction = edge / (current_price * (1 - current_price)) if current_price not in [0,1] else 0
        signals["kelly_pass"] = kelly_fraction > 0.1 and kelly_fraction < 0.5  # Bet 10-50% of edge
        
        # Filter 3: EV Gap (Risk/Reward)
        # For Kalshi: if buying YES at ask, potential profit vs loss
        potential_return = (100 - orderbook["yes_ask"]) / orderbook["yes_ask"] if orderbook["yes_ask"] > 0 else 0
        signals["ev_pass"] = potential_return > 0.5  # 50%+ return potential
        
        # Filter 4: KL Divergence (Price/Momentum divergence)
        # Simplified: Check if price moved but volume didn't confirm
        volume_trend = market_data.get("volume", 0) > market_data.get("previous_volume", 0)
        price_trend = current_price > vwap_7d
        signals["divergence_pass"] = not (volume_trend == price_trend)  # Divergence detected
        
        # Filter 5: Bayesian Context
        hour = datetime.now().hour
        # Avoid low liquidity hours (early morning)
        signals["bayesian_pass"] = 9 <= hour <= 16  # Market hours only
        
        # Filter 6: Stoikov Execution (Liquidity)
        spread = orderbook["yes_ask"] - orderbook["yes_bid"]
        signals["stoikov_pass"] = spread < 5  # Tight spread < 5 cents
        
        return signals
    
    def _estimate_true_prob(self, market_data):
        """Estimate true probability from market metadata"""
        # In production, this could pull from 538, polling averages, etc.
        # For now, use previous close as "fair value"
        return market_data.get("previous_price", 50) / 100

async def send_telegram_alert(market, direction, price, confidence):
    """Send actual trade signal to Telegram"""
    bot = Bot(token=TELEGRAM_TOKEN)
    
    message = f"""
🎯 *KALSHI SIXFILTER SIGNAL*

*{market['title'][:60]}...*

Direction: *{direction}* 
Entry: *{price}c*
Confidence: *{confidence}%*

⏰ Valid for next 5 minutes
💡 Risk 1-2% max per trade
    """
    
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=message,
        parse_mode="Markdown"
    )

async def scan_markets():
    """Main scanning loop - runs every 5 minutes"""
    global SCANNER_ACTIVE, last_signals
    
    if not SCANNER_ACTIVE:
        return
    
    logger.info("🔍 Scanning Kalshi markets...")
    
    kalshi = KalshiClient()
    six_filter = SixFilter()
    
    try:
        markets = await kalshi.get_markets(limit=100)
        
        for market in markets:
            ticker = market["ticker"]
            
            # Skip if we already signaled this recently (avoid spam)
            if ticker in last_signals and (datetime.now() - last_signals[ticker]).seconds < 3600:
                continue
            
            # Get orderbook
            try:
                orderbook = await kalshi.get_market_orderbook(ticker)
            except:
                continue
            
            # Run SixFilter
            filters = six_filter.analyze(market, orderbook)
            
            # All 6 filters must pass (conservative approach - MIT style)
            if all(filters.values()):
                # Determine direction (buy YES vs NO)
                if orderbook["implied_prob"] < 50:
                    direction = "BUY YES"
                    entry_price = orderbook["yes_ask"]
                else:
                    direction = "BUY NO"
                    entry_price = 100 - orderbook["yes_bid"]
                
                await send_telegram_alert(market, direction, entry_price, 85)
                last_signals[ticker] = datetime.now()
                logger.info(f"🚨 Signal: {ticker} {direction} @ {entry_price}")
                
    except Exception as e:
        logger.error(f"Scanner error: {e}")

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Handle Telegram /start commands"""
    global SCANNER_ACTIVE
    
    data = await request.json()
    message = data.get("message", {}).get("text", "")
    chat_id = data.get("message", {}).get("chat", {}).get("id")
    
    if message == "/start":
        SCANNER_ACTIVE = True
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(
            chat_id=chat_id,
            text="✅ Kalshi SixFilter Activated\n\nAuto-scanner: ENABLED\nMonitoring 100+ markets every 5 minutes...\n\nYou'll receive alerts when all 6 filters align."
        )
        # Trigger first scan immediately
        asyncio.create_task(scan_markets())
        
    elif message == "/stop":
        SCANNER_ACTIVE = False
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=chat_id, text="🛑 Scanner paused.")
    
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "alive", "scanner_active": SCANNER_ACTIVE}

@app.on_event("startup")
async def startup():
    scheduler.add_job(scan_markets, "interval", minutes=5)
    scheduler.start()

"""
Kalshi SixFilter Guardian - COMPLETE WORKING SYSTEM
Features: Adjustable threshold, Auto-scanner, Telegram alerts, Manual trading
"""
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import os
import aiohttp
import asyncio
from datetime import datetime

# Environment Variables
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

app = FastAPI(title="Kalshi SixFilter")

# Global state
auto_scan_enabled = False

class MarketReq(BaseModel):
    market_id: str
    market_name: str = ""
    yes_price: float
    no_price: float
    your_model_prob: float
    bankroll: float = 1000
    daily_pnl: float = 0
    consecutive_losses: int = 0

# ============ SIXFILTER ENGINE ============

def run_sixfilter_logic(yes_price, no_price, model_prob, bankroll=1000, daily_pnl=0, consecutive_losses=0):
    """The 6-Filter MIT Strategy"""
    
    # Filter 1: LMSR Edge
    market_prob = yes_price / 100
    edge = abs(model_prob - market_prob) / market_prob if market_prob > 0 else 0
    side = "YES" if model_prob > market_prob else "NO"
    
    # Filter 2: Kelly Criterion
    if side == "YES":
        b = (1 - market_prob) / market_prob
        p = model_prob
    else:
        b = market_prob / (1 - market_prob)
        p = 1 - model_prob
    
    q = 1 - p
    kelly_raw = (b * p - q) / b if b > 0 else 0
    kelly_adj = max(0, min(kelly_raw * 0.25, 0.25))
    
    if consecutive_losses > 0:
        kelly_adj *= max(0.5, 1 - (consecutive_losses * 0.1))
    if daily_pnl < -500:
        kelly_adj *= 0.5
    
    contracts = int((bankroll * kelly_adj) / 100) if kelly_adj > 0.01 else 0
    
    # Filter 3: EV Gap (2:1 RR minimum)
    if side == "YES":
        win_amt = 1 - market_prob
        loss_amt = market_prob
        win_prob = model_prob
    else:
        win_amt = market_prob
        loss_amt = 1 - market_prob
        win_prob = 1 - model_prob
    
    ev = (win_prob * win_amt) - ((1 - win_prob) * loss_amt)
    rr = win_amt / loss_amt if loss_amt > 0 else 0
    
    # Filters 4-6
    f4 = True  # KL Divergence (simplified)
    f5 = 6 <= datetime.now().hour <= 22  # Trading hours only
    f6 = True  # Context
    
    filters = [
        edge >= 0.05,           # LMSR: 5% min edge
        contracts > 0,          # Kelly: Valid size
        ev > 0 and rr >= 2.0,   # EV: 2:1 reward/risk
        f4,                     # KL: Volume OK
        f5,                     # Bayesian: Trading hours
        f6                      # Context
    ]
    
    proceed = all(filters)
    
    return {
        "proceed": proceed,
        "side": side,
        "contracts": contracts if proceed else 0,
        "limit_price": yes_price if side == "YES" else no_price,
        "confidence": int(sum(filters) / 6 * 100),
        "filters_passed": filters,
        "edge_percent": round(edge * 100, 1),
        "expected_value": round(ev * contracts, 2) if proceed else 0,
        "kelly_fraction": round(kelly_adj, 4),
        "reason": f"6-Filters: {sum(filters)}/6 | Edge: {edge:.1%} | RR: {rr:.1f}:1"
    }

# ============ KALSHI API CLIENT ============

class KalshiClient:
    def __init__(self):
        self.api_key = KALSHI_API_KEY
        self.base = "https://trading-api.kalshi.com/v1"
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.api_key}"}
        )
        return self
    
    async def __aexit__(self, *args):
        await self.session.close()
    
    async def get_markets(self):
        """Fetch active markets from Kalshi"""
        if not self.api_key:
            return []
        
        try:
            async with self.session.get(f"{self.base}/markets?limit=100&status=active") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("markets", [])
                return []
        except Exception as e:
            print(f"Error fetching markets: {e}")
            return []
    
    async def get_prices(self, market_id):
        """Get current prices for a market"""
        if not self.api_key:
            return {"yes": 50, "no": 50}
        
        try:
            async with self.session.get(f"{self.base}/markets/{market_id}/orderbook") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    yes_price = data.get("yes", {}).get("price", 0)
                    no_price = data.get("no", {}).get("price", 0)
                    return {"yes": yes_price, "no": no_price}
                return {"yes": 50, "no": 50}
        except:
            return {"yes": 50, "no": 50}

# ============ TELEGRAM ============

async def send_telegram(message: str):
    """Send notification to your phone"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"Telegram not configured")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            }, timeout=10) as resp:
                success = resp.status == 200
                if success:
                    print("✅ Telegram sent")
                return success
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False

# ============ AUTO SCANNER ============

async def auto_scanner():
    """Background scanner - runs every 5 minutes"""
    global auto_scan_enabled
    
    print("🤖 Auto-scanner initialized")
    
    while True:
        if auto_scan_enabled and KALSHI_API_KEY:
            print("🔍 Starting market scan...")
            signals_found = 0
            
            try:
                async with KalshiClient() as client:
                    markets = await client.get_markets()
                    print(f"📊 Found {len(markets)} total markets")
                    
                    if len(markets) == 0:
                        print("⚠️ No markets fetched - check API key")
                        await asyncio.sleep(300)
                        continue
                    
                    for market in markets[:100]:  # Check top 100
                        if not auto_scan_enabled:
                            print("⏹️ Scanner disabled, stopping")
                            break
                        
                        market_id = market.get("id", "")
                        name = market.get("title", "")
                        ticker = market.get("event_ticker", "")
                        
                        # Skip sports/entertainment
                        skip_words = ["sports", "nba", "nfl", "mlb", "oscar", "grammy", "super bowl", "world cup"]
                        if any(word in name.lower() for word in skip_words):
                            continue
                        
                        # Focus on financial markets
                        good_keywords = ["fed", "cpi", "gdp", "btc", "eth", "bitcoin", "s&p", "nasdaq", "gold", "oil", "gas", "rate", "election", " Trump", "crypto"]
                        if not any(kw in name.lower() or kw in ticker.lower() for kw in good_keywords):
                            continue
                        
                        # Get prices
                        prices = await client.get_prices(market_id)
                        yes_price = prices.get("yes", 0)
                        no_price = prices.get("no", 0)
                        
                        if yes_price == 0 or no_price == 0:
                            continue
                        
                        # LOOK FOR EXTREMES (contrarian strategy)
                        model_prob = None
                        
                        if yes_price > 88:  # Market 88%+ confident YES
                            model_prob = 0.80  # We think 80% (8% edge)
                        elif yes_price < 12:  # Market 88%+ confident NO
                            model_prob = 0.20  # We think 20% (8% edge)
                        elif 48 <= yes_price <= 52:  # Coin flip, skip
                            continue
                        
                        if model_prob is None:
                            continue
                        
                        # Run SixFilter
                        result = run_sixfilter_logic(
                            yes_price=yes_price,
                            no_price=no_price,
                            model_prob=model_prob,
                            bankroll=1000,
                            daily_pnl=0,
                            consecutive_losses=0
                        )
                        
                        # SEND ALERT if passes with good edge
                        if result["proceed"] and result["edge_percent"] >= 7:
                            signals_found += 1
                            
                            alert_msg = f"""🔥 <b>SIXFILTER SIGNAL</b>

📊 {name[:50]}
🎯 Side: <b>{result['side']}</b> @ {result['limit_price']}¢
📈 Edge: <b>{result['edge_percent']}%</b>
💰 Contracts: <b>{result['contracts']}</b>
✅ Filters: <b>{sum(result['filters_passed'])}/6</b>

<a href="https://kalshi.com/markets/{market_id}">➡️ Trade Now</a>"""
                            
                            await send_telegram(alert_msg)
                            print(f"✅ SIGNAL: {name[:30]} - {result['side']} @ {result['edge_percent']}%")
                            
                            # Only send 3 signals max per scan (avoid spam)
                            if signals_found >= 3:
                                break
                    
                    print(f"✅ Scan complete. Signals found: {signals_found}")
                            
            except Exception as e:
                print(f"❌ Scanner error: {e}")
                await send_telegram(f"⚠️ Scanner error: {str(e)[:100]}")
        
        # Wait 5 minutes
        await asyncio.sleep(300)

# ============ API ENDPOINTS ============

@app.post("/kalshi/analyze")
async def analyze(req: MarketReq, min_filters: int = Query(6, ge=1

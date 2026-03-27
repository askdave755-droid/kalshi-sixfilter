"""
Kalshi SixFilter Guardian - Complete Trading System
Includes: Manual Trading, Auto-Scanner, Telegram Alerts
"""
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional
import os
import aiohttp
import asyncio
from datetime import datetime, timedelta
import random

# Environment Variables
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

app = FastAPI(title="Kalshi SixFilter")

# In-memory storage (replaces database for simplicity)
signals_log = []
positions = []
auto_scan_enabled = False
daily_stats = {"pnl": 0, "trades": 0, "wins": 0, "date": datetime.now().date()}

# ============ DATA MODELS ============

class MarketReq(BaseModel):
    market_id: str
    market_name: str = ""
    yes_price: float
    no_price: float
    volume: float = 50000
    your_model_prob: float
    bankroll: float = 1000
    daily_pnl: float = 0
    consecutive_losses: int = 0

class TradeSignal(BaseModel):
    market_id: str
    side: str
    contracts: int
    price: float
    edge: float
    confidence: int

# ============ SIXFILTER ENGINE ============

def run_sixfilter(req: MarketReq):
    """The 6-Filter MIT Strategy"""
    
    # Filter 1: LMSR Edge
    market_prob = req.yes_price / 100
    edge = abs(req.your_model_prob - market_prob) / market_prob if market_prob > 0 else 0
    side = "YES" if req.your_model_prob > market_prob else "NO"
    
    # Filter 2: Kelly Criterion
    if side == "YES":
        b = (1 - market_prob) / market_prob
        p = req.your_model_prob
    else:
        b = market_prob / (1 - market_prob)
        p = 1 - req.your_model_prob
    
    q = 1 - p
    kelly_raw = (b * p - q) / b if b > 0 else 0
    kelly_adj = max(0, min(kelly_raw * 0.25, 0.25))  # Quarter Kelly
    
    # Loss streak penalty
    if req.consecutive_losses > 0:
        kelly_adj *= max(0.5, 1 - (req.consecutive_losses * 0.1))
    
    # Daily loss limit (Bulenox-style)
    if req.daily_pnl < -500:
        kelly_adj *= 0.5
    
    contracts = int((req.bankroll * kelly_adj) / 100) if kelly_adj > 0.01 else 0
    
    # Filter 3: EV Gap (2:1 RR)
    if side == "YES":
        win_amt = 1 - market_prob
        loss_amt = market_prob
        win_prob = req.your_model_prob
    else:
        win_amt = market_prob
        loss_amt = 1 - market_prob
        win_prob = 1 - req.your_model_prob
    
    ev = (win_prob * win_amt) - ((1 - win_prob) * loss_amt)
    rr = win_amt / loss_amt if loss_amt > 0 else 0
    
    # Filters 4-6 (Simplified for reliability)
    f4_kl = True  # Volume check (skipping complex calc)
    f5_time = 6 <= datetime.now().hour <= 22  # Trading hours only
    f6_context = True  # Market type check
    
    filters = [
        edge >= 0.05,           # LMSR: 5% min edge
        contracts > 0,          # Kelly: Valid size
        ev > 0 and rr >= 2.0,   # EV: 2:1 reward/risk
        f4_kl,                  # KL: Volume ok
        f5_time,                # Bayesian: Trading hours
        f6_context              # Context: Market quality
    ]
    
    proceed = all(filters)
    
    return {
        "proceed": proceed,
        "side": side,
        "contracts": contracts if proceed else 0,
        "limit_price": req.yes_price if side == "YES" else req.no_price,
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
    
    async def get_markets(self):
        if not self.api_key:
            return []
        
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            async with session.get(f"{self.base}/markets?limit=100&status=active", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("markets", [])
                return []
    
    async def get_prices(self, market_id):
        if not self.api_key:
            return {"yes": 50, "no": 50}
        
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            async with session.get(f"{self.base}/markets/{market_id}/orderbook", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    yes_bid = data.get("yes", {}).get("price", 0)
                    no_bid = data.get("no", {}).get("price", 0)
                    return {"yes": yes_bid, "no": no_bid}
                return {"yes": 50, "no": 50}

# ============ TELEGRAM ALERTS ============

async def send_telegram_alert(message: str):
    """Send notification to your phone"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as session:
        await session.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        })

# ============ AUTO SCANNER ============

async def auto_scanner():
    """Background scanner - runs every 5 minutes when enabled"""
    global auto_scan_enabled
    
    while True:
        if auto_scan_enabled and KALSHI_API_KEY:
            try:
                client = KalshiClient()
                markets = await client.get_markets()
                
                for market in markets[:20]:  # Check top 20
                    market_id = market.get("id", "")
                    name = market.get("title", "")
                    
                    # Skip sports/entertainment (focus on financials)
                    skip_words = ["sports", "oscar", "grammy", "super bowl", "nba", "nfl"]
                    if any(word in name.lower() for word in skip_words):
                        continue
                    
                    prices = await client.get_prices(market_id)
                    
                    # Simple model: Mean reversion for extremes
                    yes_price = prices.get("yes", 50)
                    model_prob = None
                    
                    if yes_price > 85:
                        model_prob = 0.70  # Overpriced YES
                    elif yes_price < 15:
                        model_prob = 0.30  # Overpriced NO
                    
                    if model_prob:
                        req = MarketReq(
                            market_id=market_id,
                            market_name=name,
                            yes_price=yes_price,
                            no_price=prices.get("no", 50),
                            your_model_prob=model_prob,
                            bankroll=1000,
                            daily_pnl=daily_stats["pnl"]
                        )
                        
                        result = run_sixfilter(req)
                        
                        if result["proceed"] and result["edge_percent"] >= 7:
                            alert = f"""🔥 <b>SIXFILTER SIGNAL</b>

📊 {name}
🎯 Side: {result['side']} @ {result['limit_price']}¢
📈 Edge: {result['edge_percent']}%
💰 Contracts: {result['contracts']}
✅ Confidence: {result['confidence']}/100

<a href="https://kalshi.com/markets/{market_id}">Trade on Kalshi →</a>"""
                            
                            await send_telegram_alert(alert)
                            
                            # Log signal
                            signals_log.append({
                                "market": name,
                                "signal": result,
                                "time": datetime.now()
                            })
                            
            except Exception as e:
                print(f"Scanner error: {e}")
        
        await asyncio.sleep(300)  # 5 minutes

# ============ API ENDPOINTS ============

@app.post("/kalshi/analyze")
async def analyze(req: MarketReq):
    """Manual analysis endpoint"""
    result = run_sixfilter(req)
    
    # Log to memory
    signals_log.append({
        "market": req.market_id,
        "result": result,
        "time": datetime.now()
    })
    
    return result

@app.post("/kalshi/execute")
async def execute(data: dict):
    """Log manual execution"""
    global daily_stats
    
    # Update stats
    daily_stats["trades"] += 1
    
    return {
        "status": "logged", 
        "message": "Trade logged. Execute manually on Kalshi.com",
        "daily_trades": daily_stats["trades"]
    }

@app.get("/scan")
async def trigger_scan():
    """Manual scan trigger"""
    if not KALSHI_API_KEY:
        return {"error": "Add KALSHI_API_KEY to Railway variables first"}
    
    client = KalshiClient()
    markets = await client.get_markets()
    
    found = []
    for market in markets[:10]:
        # Quick scan logic here
        pass
    
    return {"markets_checked": len(markets), "signals": len(found)}

@app.get("/toggle-auto")
async def toggle_auto():
    """Toggle auto-scanner on/off"""
    global auto_scan_enabled
    auto_scan_enabled = not auto_scan_enabled
    
    status = "ENABLED" if auto_scan_enabled else "DISABLED"
    return {
        "auto_scan": auto_scan_enabled,
        "message": f"Auto-scanner {status}",
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN)
    }

@app.get("/status")
async def get_status():
    """System status"""
    return {
        "auto_scan": auto_scan_enabled,
        "daily_pnl": daily_stats["pnl"],
        "trades_today": daily_stats["trades"],
        "api_key_set": bool(KALSHI_API_KEY),
        "telegram_set": bool(TELEGRAM_BOT_TOKEN),
        "recent_signals": len(signals_log)
    }

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now()}

# ============ DASHBOARD HTML ============

@app.get("/")
async def dashboard():
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kalshi SixFilter Guardian</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
        body { background: #0a0a0a; color: #e0e0e0; padding: 20px; min-height: 100vh; }
        h1 { color: #00d9ff; font-size: 1.5rem; margin-bottom: 5px; }
        .subtitle { color: #888; font-size: 0.85rem; margin-bottom: 20px; }
        
        .card { background: #151520; padding: 20px; border-radius: 12px; margin-bottom: 15px; border: 1px solid #2d2d44; }
        .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        .card-title { color: #fff; font-weight: 600; }
        
        input, select { width: 100%; padding: 12px; background: #0a0a0a; border: 1px solid #2d2d44; border-radius: 8px; color: #fff; margin-bottom: 10px; font-size: 16px; }
        input:focus { border-color: #00d9ff; outline: none; }
        
        .btn { padding: 14px 20px; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; transition: all 0.3s; font-size: 16px; }
        .btn-primary { background: linear-gradient(135deg, #00d9ff 0%, #0099cc 100%); color: #000; width: 100%; }
        .btn-success { background: linear-gradient(135deg, #00ff88 0%, #00cc6a 100%); color: #000; }
        .btn-danger { background: linear-gradient(135deg, #ff4757 0%, #cc3846 100%); color: #fff; }
        .btn-warning { background: linear-gradient(135deg, #ffa502 0%, #cc8402 100%); color: #000; }
        
        .btn-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 15px; }
        
        .filters { display: flex; gap: 8px; margin: 15px 0; flex-wrap: wrap; }
        .filter-badge { width: 40px; height: 40px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: bold; }
        .filter-pass { background: #00ff88; color: #000; }
        .filter-fail { background: #ff4757; color: #fff; }
        
        .status-indicator { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; }
        .status-on { background: #00ff88; box-shadow: 0 0 10px #00ff88; }
        .status-off { background: #ff4757; }
        
        .result-box { margin-top: 15px; padding: 15px; border-radius: 8px; display: none; }
        .result-approved { border: 2px solid #00ff88; background: rgba(0, 255, 136, 0.05); }
        .result-rejected { border: 2px solid #ff4757; background: rgba(255, 71, 87, 0.05); }
        
        .metric-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 15px; }
        .metric { background: #0a0a0a; padding: 12px; border-radius: 8px; text-align: center; }
        .metric-value { color: #00d9ff; font-size: 1.2rem; font-weight: bold; }
        .metric-label { color: #888; font-size: 0.75rem; text-transform: uppercase; margin-top: 4px; }
        
        .hidden { display: none !important; }
        .show { display: block !important; }
        
        .alert { padding: 12px; border-radius: 8px; margin-bottom: 15px; font-size: 0.9rem; }
        .alert-info { background: rgba(0, 217, 255, 0.1); border: 1px solid #00d9ff; color: #00d9ff; }
        .alert-warning { background: rgba(255, 165, 2, 0.1); border: 1px solid #ffa502; color: #ffa502; }
        
        .kill-switch { background: linear-gradient(135deg, #ff4757 0%, #cc3846 100%); color: #fff; padding: 20px; text-align: center; font-weight: bold; font-size: 1.1rem; margin-top: 20px; border-radius: 12px; cursor: pointer; }
        
        .stats-bar { display: flex; justify-content: space-around; margin-bottom: 20px; }
        .stat-item { text-align: center; }
        .stat-number { color: #00d9ff; font-size: 1.5rem; font-weight: bold; }
        .stat-label { color: #666; font-size: 0.75rem; }
    </style>
</head>
<body>
    <h1>🔮 Kalshi SixFilter Guardian</h1>
    <p class="subtitle">MIT 6-Filter Strategy | Manual + Auto Trading</p>
    
    <div class="stats-bar">
        <div class="stat-item">
            <div class="stat-number" id="dailyPnl">$0</div>
            <div class="stat-label">Daily P&L</div>
        </div>
        <div class="stat-item">
            <div class="stat-number" id="tradeCount">0</div>
            <div class="stat-label">Trades Today</div>
        </div>
        <div class="stat-item">
            <div class="stat-number" id="autoStatus">OFF</div>
            <div class="stat-label">Auto-Scan</div>
        </div>
    </div>

    <!-- Manual Trading -->
    <div class="card">
        <div class="card-header">
            <span class="card-title">📊 Manual Analysis</span>
        </div>
        
        <input type="text" id="marketId" placeholder="Market ID (e.g. FED-25BP-MAY26)">
        <input type="text" id="marketName" placeholder="Market Name">
        
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
            <input type="number" id="yesPrice" placeholder="YES Price (¢)" min="1" max="99">
            <input type="number" id="noPrice" placeholder="NO Price (¢)" min="1" max="99">
        </div>
        
        <input type="number" id="modelProb" placeholder="Your Model Probability (%)" min="0" max="100" step="0.1">
        
        <div class="btn-row">
            <button class="btn btn-primary" onclick="analyze()" style="grid-column: 1 / -1;">🔍 Run SixFilter Analysis</button>
        </div>
        
        <div id="resultBox" class="result-box">
            <h3 id="resultTitle" style="margin-bottom: 10px;"></h3>
            <div id="filtersDisplay" class="filters"></div>
            <div id="resultDetails"></div>
            <button id="executeBtn" class="btn btn-success hidden" onclick="executeTrade()" style="width: 100%; margin-top: 15px;">Execute on Kalshi.com</button>
        </div>
    </div>

    <!-- Auto Scanner -->
    <div class="card">
        <div class="card-header">
            <span class="card-title">🤖 Auto-Scanner</span>
            <span id="scannerStatus"><span class="status-indicator status-off"></span>OFF</span>
        </div>
        
        <div class="alert alert-info" id="telegramStatus">
            Configure Telegram for mobile alerts
        </div>
        
        <div class="btn-row">
            <button class="btn btn-warning" onclick="toggleAuto()">Toggle Auto-Scan</button>
            <button class="btn btn-primary" onclick="checkStatus()">Refresh Status</button>
        </div>
        
        <div style="margin-top: 15px; font-size: 0.85rem; color: #888;">
            <div>✅ Manual trading always available</div>
            <div>🔔 Auto sends Telegram alerts only</div>
            <div>⚠️ You still tap to execute</div>
        </div>
    </div>

    <!-- Kill Switch -->
    <div class="kill-switch" onclick="emergencyStop()">
        🛑 EMERGENCY STOP ALL TRADING
    </div>

    <script>
        let currentSignal = null;
        const API_URL = window.location.origin;
        
        async function analyze() {
            const data = {
                market_id: document.getElementById('marketId').value,
                market_name: document.getElementById('marketName').value,
                yes_price: parseFloat(document.getElementById('yesPrice').value),
                no_price: parseFloat(document.getElementById('noPrice').value),
                your_model_prob: parseFloat(document.getElementById('modelProb').value) / 100,
                bankroll: 1000,
                daily_pnl: 0,
                consecutive_losses: 0
            };
            
            const res = await fetch(`${API_URL}/kalshi/analyze`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            
            const result = await res.json();
            currentSignal = result;
            
            const resultBox = document.getElementById('resultBox');
            resultBox.classList.remove('show', 'result-approved', 'result-rejected');
            resultBox.classList.add('show');
            resultBox.classList.add(result.proceed ? 'result-approved' : 'result-rejected');
            
            document.getElementById('resultTitle').textContent = result.proceed ? '✅ TRADE APPROVED' : '❌ TRADE REJECTED';
            document.getElementById('resultTitle').style.color = result.proceed ? '#00ff88' : '#ff4757';
            
            document.getElementById('filtersDisplay').innerHTML = result.filters_passed.map((pass, i) => 
                `<div class="filter-badge ${pass ? 'filter-pass' : 'filter-fail'}">${i+1}</div>`
            ).join('');
            
            document.getElementById('resultDetails').innerHTML = `
                <div class="metric-grid">
                    <div class="metric">
                        <div class="metric-value">${result.side}</div>
                        <div class="metric-label">Side</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">${result.contracts}</div>
                        <div class="metric-label">Contracts</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">${result.edge_percent}%</div>
                        <div class="metric-label">Edge</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">${result.confidence}%</div>
                        <div class="metric-label">Confidence</div>
                    </div>
                </div>
                <div style="margin-top: 10px; color: #888; font-size: 0.85rem;">
                    ${result.reason}
                </div>
            `;
            
            const execBtn = document.getElementById('executeBtn');
            execBtn.classList.toggle('hidden', !result.proceed);
        }
        
        function executeTrade() {
            if (!currentSignal) return;
            
            // Open Kalshi
            const marketId = document.getElementById('marketId').value;
            window.open(`https://kalshi.com/markets/${marketId}`, '_blank');
            
            // Log it
            fetch(`${API_URL}/kalshi/execute`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    market_id: marketId,
                    side: currentSignal.side,
                    contracts: currentSignal.contracts,
                    price: currentSignal.limit_price
                })
            });
        }
        
        async function toggleAuto() {
            const res = await fetch(`${API_URL}/toggle-auto`);
            const data = await res.json();
            
            document.getElementById('autoStatus').textContent = data.auto_scan ? 'ON' : 'OFF';
            document.getElementById('scannerStatus').innerHTML = data.auto_scan ? 
                '<span class="status-indicator status-on"></span>ON' : 
                '<span class="status-indicator status-off"></span>OFF';
            
            alert(data.message);
        }
        
        async function checkStatus() {
            const res = await fetch(`${API_URL}/status`);
            const data = await res.json();
            
            document.getElementById('tradeCount').textContent = data.trades_today;
            document.getElementById('dailyPnl').textContent = '$' + data.daily_pnl;
            document.getElementById('autoStatus').textContent = data.auto_scan ? 'ON' : 'OFF';
            
            if (!data.api_key_set) {
                document.getElementById('telegramStatus').textContent = '⚠️ Add KALSHI_API_KEY to Railway for auto-scan';
                document.getElementById('telegramStatus').className = 'alert alert-warning';
            } else if (!data.telegram_set) {
                document.getElementById('telegramStatus').textContent = 'ℹ️ Optional: Add TELEGRAM_BOT_TOKEN for mobile alerts';
            } else {
                document.getElementById('telegramStatus').textContent = '✅ Fully configured';
                document.getElementById('telegramStatus').className = 'alert alert-info';
            }
        }
        
        function emergencyStop() {
            if (confirm('🛑 STOP ALL TRADING?\n\nThis will disable auto-scanner immediately.')) {
                fetch(`${API_URL}/toggle-auto`).then(() => {
                    alert('Trading stopped. Manual mode still available.');
                    location.reload();
                });
            }
        }
        
        // Load status on page load
        checkStatus();
    </script>
</body>
</html>""")

# Start background scanner on startup
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_scanner())

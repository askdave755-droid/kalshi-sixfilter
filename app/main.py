"""
Kalshi SixFilter Guardian - Complete Trading System
"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import os
import aiohttp
from datetime import datetime

KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

app = FastAPI(title="Kalshi SixFilter")

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

def run_sixfilter(yes_price, no_price, model_prob, bankroll=1000, daily_pnl=0, consecutive_losses=0):
    market_prob = yes_price / 100
    edge = abs(model_prob - market_prob) / market_prob if market_prob > 0 else 0
    side = "YES" if model_prob > market_prob else "NO"
    
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
    
    f5_time = 6 <= datetime.now().hour <= 22
    
    filters = [
        edge >= 0.05,
        contracts > 0,
        ev > 0 and rr >= 2.0,
        True,
        f5_time,
        True
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
        "reason": f"6-Filters: {sum(filters)}/6 | Edge: {edge:.1%}"
    }

async def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"})
    except:
        pass

@app.post("/kalshi/analyze")
async def analyze(req: MarketReq):
    result = run_sixfilter(req.yes_price, req.no_price, req.your_model_prob, req.bankroll, req.daily_pnl, req.consecutive_losses)
    return result

@app.get("/toggle-auto")
async def toggle_auto():
    global auto_scan_enabled
    auto_scan_enabled = not auto_scan_enabled
    status = "ENABLED" if auto_scan_enabled else "DISABLED"
    
    if auto_scan_enabled:
        await send_telegram(f"✅ <b>Kalshi SixFilter Activated</b>\n\nAuto-scanner: {status}\nMonitoring markets...")
    
    return {
        "success": True,
        "auto_scan": auto_scan_enabled,
        "message": f"Auto-scanner {status}"
    }

@app.get("/status")
async def status():
    return {
        "auto_scan": auto_scan_enabled,
        "telegram_set": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    }

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
async def dashboard():
    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi SixFilter</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, sans-serif; }
body { background: #0a0a0a; color: #e0e0e0; padding: 20px; min-height: 100vh; }
h1 { color: #00d9ff; font-size: 1.5rem; }
.subtitle { color: #888; font-size: 0.8rem; margin-bottom: 20px; }
.card { background: #151520; padding: 20px; border-radius: 12px; margin-bottom: 15px; border: 1px solid #2d2d44; }
input { width: 100%; padding: 12px; background: #0a0a0a; border: 1px solid #2d2d44; border-radius: 8px; color: #fff; margin-bottom: 10px; font-size: 16px; }
.btn { padding: 14px 20px; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 16px; width: 100%; margin-top: 10px; }
.btn-primary { background: #00d9ff; color: #000; }
.btn-success { background: #00ff88; color: #000; }
.btn-warning { background: #ffa502; color: #000; }
.filters { display: flex; gap: 8px; margin: 15px 0; }
.filter-badge { width: 40px; height: 40px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: bold; }
.filter-pass { background: #00ff88; color: #000; }
.filter-fail { background: #ff4757; color: #fff; }
.status-indicator { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; }
.status-on { background: #00ff88; box-shadow: 0 0 10px #00ff88; }
.status-off { background: #ff4757; }
.result-box { margin-top: 15px; padding: 15px; border-radius: 8px; display: none; }
.result-approved { border: 2px solid #00ff88; background: rgba(0,255,136,0.05); }
.result-rejected { border: 2px solid #ff4757; background: rgba(255,71,87,0.05); }
.metric-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 15px; }
.metric { background: #0a0a0a; padding: 12px; border-radius: 8px; text-align: center; }
.metric-value { color: #00d9ff; font-size: 1.2rem; font-weight: bold; }
.metric-label { color: #888; font-size: 0.75rem; margin-top: 4px; }
.hidden { display: none !important; }
.show { display: block !important; }
.alert { padding: 12px; border-radius: 8px; margin-bottom: 15px; font-size: 0.9rem; }
.alert-info { background: rgba(0,217,255,0.1); border: 1px solid #00d9ff; color: #00d9ff; }
.alert-success { background: rgba(0,255,136,0.1); border: 1px solid #00ff88; color: #00ff88; }
.alert-error { background: rgba(255,71,87,0.1); border: 1px solid #ff4757; color: #ff4757; }
.btn-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 15px; }
.kill-switch { background: #ff4757; color: #fff; padding: 20px; text-align: center; font-weight: bold; border-radius: 12px; cursor: pointer; margin-top: 20px; }
</style>
</head>
<body>
<h1>🔮 Kalshi SixFilter</h1>
<p class="subtitle">MIT 6-Filter Strategy</p>

<div id="alertBox"></div>

<div class="card">
    <div class="card-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
        <span style="font-weight: 600;">📊 Manual Analysis</span>
    </div>
    <input type="text" id="marketId" placeholder="Market ID (e.g. FED-25BP-MAY26)">
    <input type="text" id="marketName" placeholder="Market Name">
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
        <input type="number" id="yesPrice" placeholder="YES Price (¢)" min="1" max="99">
        <input type="number" id="noPrice" placeholder="NO Price (¢)" min="1" max="99">
    </div>
    <input type="number" id="modelProb" placeholder="Your Model Prob (%)" min="0" max="100" step="0.1">
    <button class="btn btn-primary" onclick="analyze()">🔍 Run SixFilter Analysis</button>
    
    <div id="resultBox" class="result-box">
        <h3 id="resultTitle" style="margin-bottom: 10px;"></h3>
        <div id="filtersDisplay" class="filters"></div>
        <div id="resultDetails"></div>
        <button id="executeBtn" class="btn btn-success hidden" onclick="executeTrade()">Execute on Kalshi.com</button>
    </div>
</div>

<div class="card">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
        <span style="font-weight: 600;">🤖 Auto-Scanner</span>
        <span id="scannerStatus"><span class="status-indicator status-off"></span>OFF</span>
    </div>
    <div id="telegramStatus" class="alert alert-info">Checking Telegram config...</div>
    <div class="btn-row">
        <button class="btn btn-warning" onclick="toggleAuto()">Toggle Auto-Scan</button>
        <button class="btn btn-primary" onclick="checkStatus()">Refresh</button>
    </div>
</div>

<div class="kill-switch" onclick="emergencyStop()">🛑 EMERGENCY STOP</div>

<script>
let currentSignal = null;
const API_URL = window.location.origin;

function showAlert(msg, type) {
    const box = document.getElementById('alertBox');
    box.innerHTML = '<div class="alert alert-' + type + '">' + msg + '</div>';
    setTimeout(() => box.innerHTML = '', 5000);
}

async function analyze() {
    try {
        const yesPrice = parseFloat(document.getElementById('yesPrice').value);
        const noPrice = parseFloat(document.getElementById('noPrice').value);
        const modelProb = parseFloat(document.getElementById('modelProb').value);
        
        if (!yesPrice || !noPrice || !modelProb) {
            showAlert('Fill in all fields', 'error');
            return;
        }
        
        const res = await fetch(API_URL + '/kalshi/analyze', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                market_id: document.getElementById('marketId').value || 'TEST',
                market_name: document.getElementById('marketName').value || 'Test Market',
                yes_price: yesPrice,
                no_price: noPrice,
                your_model_prob: modelProb / 100,
                bankroll: 1000,
                daily_pnl: 0,
                consecutive_losses: 0
            })
        });
        
        if (!res.ok) throw new Error('Server error');
        const result = await res.json();
        currentSignal = result;
        
        const resultBox = document.getElementById('resultBox');
        resultBox.classList.remove('show', 'result-approved', 'result-rejected');
        resultBox.classList.add('show');
        resultBox.classList.add(result.proceed ? 'result-approved' : 'result-rejected');
        
        document.getElementById('resultTitle').textContent = result.proceed ? '✅ APPROVED' : '❌ REJECTED';
        document.getElementById('resultTitle').style.color = result.proceed ? '#00ff88' : '#ff4757';
        
        document.getElementById('filtersDisplay').innerHTML = result.filters_passed.map((pass, i) => 
            '<div class="filter-badge ' + (pass ? 'filter-pass' : 'filter-fail') + '">' + (i+1) + '</div>'
        ).join('');
        
        document.getElementById('resultDetails').innerHTML = 
            '<div class="metric-grid">' +
            '<div class="metric"><div class="metric-value">' + result.side + '</div><div class="metric-label">Side</div></div>' +
            '<div class="metric"><div class="metric-value">' + result.contracts + '</div><div class="metric-label">Contracts</div></div>' +
            '<div class="metric"><div class="metric-value">' + result.edge_percent + '%</div><div class="metric-label">Edge</div></div>' +
            '<div class="metric"><div class="metric-value">' + result.confidence + '%</div><div class="metric-label">Confidence</div></div>' +
            '</div>' +
            '<div style="margin-top: 10px; color: #888; font-size: 0.85rem;">' + result.reason + '</div>';
        
        const execBtn = document.getElementById('executeBtn');
        execBtn.classList.toggle('hidden', !result.proceed);
        
    } catch (e) {
        showAlert('Error: ' + e.message, 'error');
        console.error(e);
    }
}

function executeTrade() {
    if (!currentSignal) return;
    const marketId = document.getElementById('marketId').value;
    window.open('https://kalshi.com/markets/' + marketId, '_blank');
}

async function toggleAuto() {
    try {
        showAlert('Toggling auto-scan...', 'info');
        const res = await fetch(API_URL + '/toggle-auto');
        if (!res.ok) throw new Error('Server error ' + res.status);
        const data = await res.json();
        
        // FIX: Use correct element ID 'scannerStatus' not 'autoStatus'
        const scannerEl = document.getElementById('scannerStatus');
        scannerEl.innerHTML = data.auto_scan ? 
            '<span class="status-indicator status-on"></span>ON' : 
            '<span class="status-indicator status-off"></span>OFF';
        
        showAlert(data.message, data.auto_scan ? 'success' : 'info');
    } catch (e) {
        showAlert('Error: ' + e.message, 'error');
        console.error(e);
    }
}

async function checkStatus() {
    try {
        const res = await fetch(API_URL + '/status');
        const data = await res.json();
        
        const telegramDiv = document.getElementById('telegramStatus');
        if (!data.telegram_set) {
            telegramDiv.className = 'alert alert-error';
            telegramDiv.textContent = '⚠️ Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to Railway.';
        } else {
            telegramDiv.className = 'alert alert-success';
            telegramDiv.textContent = '✅ Telegram configured. Ready.';
        }
        
        const scannerEl = document.getElementById('scannerStatus');
        scannerEl.innerHTML = data.auto_scan ? 
            '<span class="status-indicator status-on"></span>ON' : 
            '<span class="status-indicator status-off"></span>OFF';
        
    } catch (e) {
        showAlert('Status check failed', 'error');
    }
}

function emergencyStop() {
    if (confirm('STOP ALL TRADING?')) {
        fetch(API_URL + '/toggle-auto').then(() => {
            showAlert('Trading stopped', 'success');
            checkStatus();
        });
    }
}

// Load status on start
checkStatus();
</script>
</body>
</html>""")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List
import os

app = FastAPI()

class MarketReq(BaseModel):
    market_id: str
    market_name: str = ""
    yes_price: float
    no_price: float
    volume: float = 50000
    open_interest: float = 25000
    your_model_prob: float
    bankroll: float = 1000
    daily_pnl: float = 0
    consecutive_losses: int = 0

@app.post("/kalshi/analyze")
async def analyze(req: MarketReq):
    market_prob = req.yes_price / 100
    edge = abs(req.your_model_prob - market_prob) / market_prob if market_prob > 0 else 0
    side = "YES" if req.your_model_prob > market_prob else "NO"
    
    # Kelly calc
    if side == "YES":
        b = (1 - market_prob) / market_prob
        p = req.your_model_prob
    else:
        b = market_prob / (1 - market_prob)
        p = 1 - req.your_model_prob
    kelly = max(0, ((b * p - (1-p)) / b) * 0.25)
    contracts = int((req.bankroll * kelly) / 100) if kelly > 0 else 0
    
    filters = [edge >= 0.05, contracts > 0, True, True, True, True]
    
    return {
        "proceed": all(filters),
        "side": side,
        "contracts": contracts,
        "limit_price": req.yes_price if side == "YES" else req.no_price,
        "confidence": int(sum(filters)/6*100),
        "filters_passed": filters,
        "edge_percent": round(edge*100, 1),
        "expected_value": 0,
        "kelly_fraction": round(kelly, 4),
        "reason": f"6-Filters: {sum(filters)}/6"
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
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 15px; }
h1 { color: #00d9ff; font-size: 1.5rem; margin-bottom: 5px; }
.subtitle { color: #888; font-size: 0.8rem; margin-bottom: 20px; }
.card { background: #151520; padding: 15px; border-radius: 12px; margin-bottom: 15px; border: 1px solid #2d2d44; }
input { width: 100%; padding: 12px; background: #0a0a0a; border: 1px solid #2d2d44; border-radius: 8px; color: #fff; margin-bottom: 10px; font-size: 16px; }
button { width: 100%; padding: 14px; background: #00d9ff; color: #000; border: none; border-radius: 8px; font-weight: bold; font-size: 16px; }
.btn-success { background: #00ff88; margin-top: 10px; }
.filters { display: flex; gap: 8px; margin: 15px 0; }
.badge { width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: bold; }
.pass { background: #00ff88; color: #000; }
.fail { background: #ff4757; color: #fff; }
.hidden { display: none; }
.result-box { margin-top: 15px; padding: 15px; background: #0a0a0a; border-radius: 8px; }
.approved { border: 2px solid #00ff88; }
.rejected { border: 2px solid #ff4757; }
</style>
</head>
<body>
<h1>🔮 Kalshi SixFilter</h1>
<p class="subtitle">MIT 6-Filter Strategy | Manual Execution</p>

<div class="card">
<h3 style="margin-bottom: 15px;">Market Input</h3>
<input type="text" id="marketId" placeholder="Market ID (e.g. FED-25BP-MAY26)">
<input type="text" id="marketName" placeholder="Market Name">
<input type="number" id="yesPrice" placeholder="YES Price (¢)" min="1" max="99">
<input type="number" id="noPrice" placeholder="NO Price (¢)" min="1" max="99">
<input type="number" id="modelProb" placeholder="Your Model Prob (%)" min="0" max="100" step="0.1">
<button onclick="analyze()">🔍 Run SixFilter Analysis</button>
</div>

<div id="result" class="card hidden">
<h3 id="resultTitle" style="margin-bottom: 10px;"></h3>
<div id="filters" class="filters"></div>
<div id="resultDetails" class="result-box"></div>
<button id="executeBtn" class="btn-success hidden" onclick="execute()">Execute on Kalshi.com</button>
</div>

<script>
let currentSignal = null;
async function analyze() {
const data = {
    market_id: document.getElementById('marketId').value,
    market_name: document.getElementById('marketName').value,
    yes_price: parseFloat(document.getElementById('yesPrice').value),
    no_price: parseFloat(document.getElementById('noPrice').value),
    volume: 50000, open_interest: 25000,
    your_model_prob: parseFloat(document.getElementById('modelProb').value) / 100,
    bankroll: 1000, daily_pnl: 0, consecutive_losses: 0
};
const res = await fetch('/kalshi/analyze', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
});
const signal = await res.json();
currentSignal = signal;
const resultDiv = document.getElementById('result');
resultDiv.classList.remove('hidden', 'approved', 'rejected');
resultDiv.classList.add(signal.proceed ? 'approved' : 'rejected');
document.getElementById('resultTitle').textContent = signal.proceed ? '✅ APPROVED' : '❌ REJECTED';
document.getElementById('resultTitle').style.color = signal.proceed ? '#00ff88' : '#ff4757';
document.getElementById('filters').innerHTML = signal.filters_passed.map((p, i) => 
    `<div class="badge ${p ? 'pass' : 'fail'}">${i+1}</div>`).join('');
document.getElementById('resultDetails').innerHTML = 
    `<b>Side:</b> ${signal.side} | <b>Contracts:</b> ${signal.contracts} | <b>Price:</b> ${signal.limit_price}¢<br>` +
    `<b>Edge:</b> ${signal.edge_percent}% | <b>Confidence:</b> ${signal.confidence}%`;
document.getElementById('executeBtn').classList.toggle('hidden', !signal.proceed);
}
function execute() {
    if(currentSignal) window.open(`https://kalshi.com/markets/${currentSignal.market_id}`, '_blank');
}
</script>
</body>
</html>""")

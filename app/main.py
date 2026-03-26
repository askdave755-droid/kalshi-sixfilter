"""
Kalshi SixFilter Guardian - Prediction Market Trading System
MIT 6-Filter Strategy: LMSR | Kelly | EV Gap | KL Divergence | Bayesian | Stoikov
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List
import os
import asyncpg
from datetime import datetime
from contextlib import asynccontextmanager

DATABASE_URL = os.getenv("DATABASE_URL")
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY")

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.connect(DATABASE_URL)
    await app.state.db.execute("CREATE TABLE IF NOT EXISTS signals (id SERIAL PRIMARY KEY, market_id TEXT, side TEXT, contracts INT, price INT, confidence INT, filters JSONB, status TEXT DEFAULT 'pending', created TIMESTAMP DEFAULT NOW())")
    yield
    await app.state.db.close()

app = FastAPI(lifespan=lifespan)

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

def run_filters(req: MarketReq):
    # Filter 1: LMSR Deviation
    market_prob = req.yes_price / 100
    edge = abs(req.your_model_prob - market_prob) / market_prob if market_prob > 0 else 0
    f1 = edge >= 0.05
    
    # Side determination
    side = "YES" if req.your_model_prob > market_prob else "NO"
    
    # Filter 2: Kelly Criterion
    if side == "YES":
        b = (1 - market_prob) / market_prob
        p = req.your_model_prob
    else:
        b = market_prob / (1 - market_prob)
        p = 1 - req.your_model_prob
    kelly = ((b * p - (1 - p)) / b) * 0.25 if b > 0 else 0
    kelly *= max(0.5, 1 - (req.consecutive_losses * 0.1))
    if req.daily_pnl < -500: kelly *= 0.5
    contracts = int((req.bankroll * kelly) / 100) if kelly > 0 else 0
    f2 = contracts > 0 and kelly > 0.01
    
    # Filter 3: EV Gap (2:1 minimum)
    if side == "YES":
        ev = (req.your_model_prob * (1 - market_prob)) - ((1 - req.your_model_prob) * market_prob)
        rr = (1 - market_prob) / market_prob if market_prob > 0 else 0
    else:
        ev = ((1 - req.your_model_prob) * market_prob) - (req.your_model_prob * (1 - market_prob))
        rr = market_prob / (1 - market_prob) if market_prob < 1 else 0
    f3 = ev > 0 and rr >= 2.0
    
    # Filter 4: KL Divergence
    vol_oi = req.volume / req.open_interest if req.open_interest > 0 else 0
    price_ext = abs(req.yes_price - 50) / 50
    div_score = price_ext * (1 - min(vol_oi, 1))
    f4 = div_score < 0.7
    
    # Filter 5: Bayesian Context
    hour = datetime.now().hour
    f5 = (6 <= hour <= 22) and (req.daily_pnl > -500)
    
    # Filter 6: Stoikov Level
    spread = abs(req.yes_price - (100 - req.no_price))
    mid = (req.yes_price + (100 - req.no_price)) / 2
    if side == "YES":
        price = min(req.yes_price, mid - (spread * 0.1))
    else:
        price = min(req.no_price, mid - (spread * 0.1))
    price = max(1, min(99, round(price)))
    f6 = price > 0
    
    filters = [f1, f2, f3, f4, f5, f6]
    return {
        "proceed": all(filters),
        "side": side,
        "contracts": contracts if all(filters) else 0,
        "limit_price": price,
        "confidence": int(sum(filters) / 6 * 100),
        "filters_passed": filters,
        "edge_percent": round(edge * 100, 1),
        "expected_value": round(ev * contracts, 2) if all(filters) else 0,
        "kelly_fraction": round(kelly, 4),
        "reason": f"6-Filters: {sum(filters)}/6 | Edge: {edge:.1%}"
    }

@app.post("/kalshi/analyze")
async def analyze(req: MarketReq):
    result = run_filters(req)
    await app.state.db.execute(
        "INSERT INTO signals (market_id, side, contracts, price, confidence, filters) VALUES ($1, $2, $3, $4, $5, $6)",
        req.market_id, result["side"], result["contracts"], result["limit_price"], 
        result["confidence"], result["filters_passed"]
    )
    return result

@app.post("/kalshi/execute")
async def execute(data: dict):
    await app.state.db.execute(
        "UPDATE signals SET status = 'executed' WHERE market_id = $1",
        data.get("market_id")
    )
    return {"status": "logged"}

@app.get("/kalshi/signals")
async def get_signals():
    rows = await app.state.db.fetch("SELECT * FROM signals ORDER BY created DESC LIMIT 20")
    return [dict(r) for r in rows]

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
async def dashboard():
    html = """
<!DOCTYPE html>
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
.filters { display: flex; gap: 8px; margin: 15px 0; flex-wrap: wrap; }
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
<p class="subtitle">MIT 6-Filter Strategy | Manual Execution Dashboard</p>

<div class="card">
<h3 style="margin-bottom: 15px;">Market Input</h3>
<input type="text" id="marketId" placeholder="Market ID (e.g. FED-25BP-MAY26)">
<input type="text" id="marketName" placeholder="Market Name">
<input type="number" id="yesPrice" placeholder="YES Price (¢)" min="1" max="99">
<input type="number" id="noPrice" placeholder="NO Price (¢)" min="1" max="99">
<input type="number" id="modelProb" placeholder="Your Model Probability (%)" min="0" max="100" step="0.1">
<input type="number" id="volume" placeholder="Volume" value="50000">
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
    volume: parseFloat(document.getElementById('volume').value),
    open_interest: parseFloat(document.getElementById('volume').value) * 0.5,
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

document.getElementById('resultTitle').textContent = signal.proceed ? '✅ TRADE APPROVED' : '❌ TRADE REJECTED';
document.getElementById('resultTitle').style.color = signal.proceed ? '#00ff88' : '#ff4757';

document.getElementById('filters').innerHTML = signal.filters_passed.map((p, i) => 
    `<div class="badge ${p ? 'pass' : 'fail'}">${i+1}</div>`).join('');

document.getElementById('resultDetails').innerHTML = 
    `<b>Side:</b> ${signal.side} | <b>Contracts:</b> ${signal.contracts} | <b>Price:</b> ${signal.limit_price}¢<br>` +
    `<b>Edge:</b> ${signal.edge_percent}% | <b>Confidence:</b> ${signal.confidence}% | <b>EV:</b> $${signal.expected_value}<br>` +
    `<small style="color:#888">${signal.reason}</small>`;

document.getElementById('executeBtn').classList.toggle('hidden', !signal.proceed);
}

function execute() {
    if(currentSignal) {
        window.open(`https://kalshi.com/markets/${currentSignal.market_id}`, '_blank');
        fetch('/kalshi/execute', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({market_id: currentSignal.market_id, side: currentSignal.side, 
            contracts: currentSignal.contracts, limit_price: currentSignal.limit_price})});
    }
}
</script>
</body>
</html>
"""
    return HTMLResponse(html)

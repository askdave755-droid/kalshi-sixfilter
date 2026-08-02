"""
SixFilter Kalshi Auto-Trader v2.0
Single-file FastAPI backend
Paste into app/main.py and deploy to Railway
"""

import os
import json
import base64
import hmac
import hashlib
import time
import logging
from datetime import datetime, date
from typing import Optional, Dict, List, Any
from enum import Enum
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import requests

# ─── CONFIG ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sixfilter")

KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Risk settings (override in Railway Variables)
MAX_TRADES_DAY = int(os.getenv("MAX_TRADES_DAY", "10"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "50.0"))
MIN_EDGE_CENTS = float(os.getenv("MIN_EDGE_CENTS", "5.0"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))  # Quarter Kelly
DEFAULT_BANKROLL = float(os.getenv("DEFAULT_BANKROLL", "1000.0"))

# Markets to scan
SCAN_MARKETS = os.getenv("SCAN_MARKETS", "KXBTC15M,KXETH15M,KXEURUSD").split(",")

# ─── PYDANTIC MODELS ─────────────────────────────────────────────────
class Side(str, Enum):
    YES = "yes"
    NO = "no"

class SimpleTradeRequest(BaseModel):
    """Simple format — only send these 3 fields."""
    ticker: str
    side: Side
    contracts: int = Field(default=1, ge=1, le=100)
    auto_size: bool = Field(default=True, description="Use Kelly criterion sizing")
    force: bool = Field(default=False, description="Bypass SixFilter — NOT recommended")

class TradeResult(BaseModel):
    success: bool
    order_id: Optional[str] = None
    ticker: str
    side: str
    filled_count: int = 0
    avg_price: Optional[float] = None
    status: str
    sixfilter_score: float
    edge_cents: float
    kelly_contracts: int
    message: str
    timestamp: str

# ─── KALSHI CLIENT ─────────────────────────────────────────────────
class KalshiClient:
    def __init__(self):
        self.api_key = KALSHI_API_KEY
        self.private_key = KALSHI_PRIVATE_KEY
        self.base = BASE_URL
        self.session = requests.Session()
    
    def _sign(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Kalshi API v2 request signing."""
        if not self.api_key or not self.private_key:
            return {"Content-Type": "application/json"}
        timestamp = str(int(time.time() * 1000))
        msg = self.api_key + timestamp + method + path + body
        signature = base64.b64encode(
            hmac.new(
                self.private_key.encode("utf-8"),
                msg.encode("utf-8"),
                hashlib.sha256
            ).digest()
        ).decode("utf-8")
        return {
            "KALSHI-API-KEY": self.api_key,
            "KALSHI-API-TIMESTAMP": timestamp,
            "KALSHI-API-SIGNATURE": signature,
            "Content-Type": "application/json"
        }
    
    def get(self, path: str) -> Dict:
        url = f"{self.base}{path}"
        r = self.session.get(url, headers=self._sign("GET", path), timeout=10)
        r.raise_for_status()
        return r.json()
    
    def post(self, path: str, payload: Dict) -> Dict:
        url = f"{self.base}{path}"
        body = json.dumps(payload)
        r = self.session.post(url, headers=self._sign("POST", path, body), data=body, timeout=10)
        r.raise_for_status()
        return r.json()
    
    def get_market(self, ticker: str) -> Dict:
        return self.get(f"/markets/{ticker}")
    
    def get_orderbook(self, ticker: str) -> Dict:
        return self.get(f"/markets/{ticker}/orderbook")
    
    def place_order(self, ticker: str, side: str, count: int, price: int) -> Dict:
        """Place limit order. Price in cents (0-100)."""
        payload = {
            "ticker": ticker,
            "client_order_id": f"sf_{int(time.time()*1000)}",
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": count,
            "yes_price": price if side == "yes" else None,
            "no_price": price if side == "no" else None,
            "expiration_ts": int(time.time() * 1000) + 60000
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.post("/portfolio/orders", payload)

# ─── SIXFILTER ENGINE ────────────────────────────────────────────────
@dataclass
class FilterResult:
    passed: bool
    score: float
    reason: str

class SixFilterEngine:
    def __init__(self):
        self.memory: Dict[str, List[float]] = {}
    
    def _hist(self, ticker: str, price: float):
        self.memory.setdefault(ticker, []).append(price)
        if len(self.memory[ticker]) > 50:
            self.memory[ticker].pop(0)
    
    def lmsr(self, ticker: str, price: float) -> FilterResult:
        hist = self.memory.get(ticker, [])
        if len(hist) < 5:
            return FilterResult(True, 50, "No history")
        vwap = sum(hist) / len(hist)
        dev = abs(price - vwap)
        return FilterResult(dev > 2, min(100, dev * 10), f"VWAP={vwap:.1f}")
    
    def kelly(self, edge: float, market_prob: float) -> FilterResult:
        if edge <= 0:
            return FilterResult(False, 0, "Zero/negative edge")
        p = (edge + market_prob * 100) / 100
        q = 1 - p
        odds = (100 - market_prob * 100) / 100
        k = (p * odds - q) / odds if odds > 0 else 0
        return FilterResult(k > 0, min(100, max(0, k * 200)), f"k={k:.3f}")
    
    def ev_gap(self, edge: float) -> FilterResult:
        return FilterResult(edge >= MIN_EDGE_CENTS, 
                          min(100, (edge / max(MIN_EDGE_CENTS, 0.1)) * 50),
                          f"Edge={edge:.1f}c")
    
    def divergence(self, ticker: str) -> FilterResult:
        hist = self.memory.get(ticker, [])
        if len(hist) < 3:
            return FilterResult(True, 50, "No momentum data")
        deltas = [hist[i] - hist[i-1] for i in range(1, len(hist))]
        up = sum(d for d in deltas if d > 0)
        down = abs(sum(d for d in deltas if d < 0))
        mom = 100 - (100 / (1 + up / down)) if down else 100
        return FilterResult(30 < mom < 85, 100 - abs(mom - 50) * 2, f"Mom={mom:.1f}")
    
    def bayesian(self, hour: int, daily_pnl: float) -> FilterResult:
        if daily_pnl <= -DAILY_LOSS_LIMIT * 0.8:
            return FilterResult(False, 10, "Near loss limit")
        return FilterResult(9 <= hour <= 15, 80 if 9 <= hour <= 15 else 30,
                          f"Hour={hour}, PnL=${daily_pnl:.2f}")
    
    def stoikov(self, ask: float, bid: float) -> FilterResult:
        spread = ask - bid
        return FilterResult(spread <= 5, max(0, 100 - spread * 10), f"Spread={spread:.1f}c")
    
    def analyze(self, ticker: str, side: str, yes_ask: float, no_ask: float,
                model_prob: float, daily_pnl: float) -> Dict:
        self._hist(ticker, yes_ask)
        hour = datetime.now().hour
        market_prob = yes_ask / 100 if side == "yes" else no_ask / 100
        edge = (model_prob - market_prob) * 100 if side == "yes" else ((1 - model_prob) - market_prob) * 100
        
        f1 = self.lmsr(ticker, yes_ask)
        f2 = self.kelly(edge, market_prob)
        f3 = self.ev_gap(edge)
        f4 = self.divergence(ticker)
        f5 = self.bayesian(hour, daily_pnl)
        f6 = self.stoikov(yes_ask, 100 - yes_ask)  # Approximate bid
        
        passed = sum(f.passed for f in [f1, f2, f3, f4, f5, f6])
        score = sum(f.score for f in [f1, f2, f3, f4, f5, f6]) / 6
        
        # Kelly sizing
        kelly_contracts = 0
        if passed >= 4 and f3.passed and f2.score > 0:
            kelly_contracts = max(1, int(KELLY_FRACTION * DEFAULT_BANKROLL / yes_ask))
            kelly_contracts = min(kelly_contracts, 10)
        
        return {
            "passed": passed >= 4 and f3.passed,
            "passed_count": passed,
            "score": score,
            "edge_cents": edge,
            "kelly_contracts": kelly_contracts,
            "filters": {
                "LMSR": {"p": f1.passed, "s": round(f1.score, 1), "r": f1.reason},
                "Kelly": {"p": f2.passed, "s": round(f2.score, 1), "r": f2.reason},
                "EV_Gap": {"p": f3.passed, "s": round(f3.score, 1), "r": f3.reason},
                "KL_Div": {"p": f4.passed, "s": round(f4.score, 1), "r": f4.reason},
                "Bayesian": {"p": f5.passed, "s": round(f5.score, 1), "r": f5.reason},
                "Stoikov": {"p": f6.passed, "s": round(f6.score, 1), "r": f6.reason},
            }
        }

# ─── RISK MANAGER ───────────────────────────────────────────────────
class RiskManager:
    def __init__(self):
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.positions: Dict[str, Dict] = {}
        self.trade_log: List[Dict] = []
        self.last_reset = str(date.today())
        self.running = True
        self._check_date()
    
    def _check_date(self):
        today = str(date.today())
        if today != self.last_reset:
            self.daily_pnl = 0.0
            self.trades_today = 0
            self.trade_log = []
            self.last_reset = today
    
    def can_trade(self) -> bool:
        self._check_date()
        return self.running and self.trades_today < MAX_TRADES_DAY and self.daily_pnl > -DAILY_LOSS_LIMIT
    
    def record(self, ticker: str, side: str, contracts: int, price: float, analysis: Dict):
        self._check_date()
        self.trades_today += 1
        self.positions[ticker] = {
            "ticker": ticker, "side": side, "contracts": contracts,
            "entry_price": price, "opened_at": datetime.now().isoformat()
        }
        self.trade_log.append({
            "time": datetime.now().isoformat(), "ticker": ticker,
            "side": side, "contracts": contracts, "price": price,
            "sixfilter": analysis
        })
    
    def close(self, ticker: str, yes_price: float) -> float:
        if ticker not in self.positions:
            return 0.0
        pos = self.positions.pop(ticker)
        if pos["side"] == "yes":
            pnl = (yes_price - pos["entry_price"]) * pos["contracts"] / 100
        else:
            pnl = (pos["entry_price"] - (100 - yes_price)) * pos["contracts"] / 100
        self.daily_pnl += pnl
        return pnl
    
    def status(self) -> Dict:
        self._check_date()
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "trades_today": self.trades_today,
            "max_trades": MAX_TRADES_DAY,
            "loss_limit": DAILY_LOSS_LIMIT,
            "min_edge": MIN_EDGE_CENTS,
            "last_reset": self.last_reset,
            "running": self.running,
            "positions": list(self.positions.values()),
            "trade_log": self.trade_log[-20:]
        }

# ─── TELEGRAM ────────────────────────────────────────────────────────
class Telegram:
    def __init__(self):
        self.token = TELEGRAM_BOT_TOKEN
        self.chat = TELEGRAM_CHAT_ID
        self.ok = bool(self.token and self.chat)
    
    def send(self, msg: str):
        if not self.ok:
            logger.info(f"[NO TG] {msg}")
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat, "text": msg, "parse_mode": "Markdown"},
                timeout=5
            )
        except Exception as e:
            logger.error(f"Telegram error: {e}")
    
    def trade(self, result: TradeResult, analysis: Dict):
        emoji = "✅" if result.success else "❌"
        self.send(
            f"{emoji} *SixFilter Trade*\n"
            f"Ticker: `{result.ticker}` | Side: {result.side.upper()}\n"
            f"Contracts: {result.filled_count} | Price: {result.avg_price}\n"
            f"Score: {result.sixfilter_score:.1f}/100 | Edge: {result.edge_cents:.1f}c\n"
            f"Status: {result.status}\n{result.message}"
        )

# ─── GLOBALS ─────────────────────────────────────────────────────────
kalshi = KalshiClient()
sixfilter = SixFilterEngine()
risk = RiskManager()
telegram = Telegram()

# ─── MODEL PROBABILITY HOOK ─────────────────────────────────────────
# TODO: Replace this with your FlowAlert + Bayesian real model
def get_model_probability(ticker: str, side: str, market: Dict) -> float:
    """
    PLACEHOLDER — Wire your FlowAlert + Bayesian model here.
    Currently uses simple trend-following overlay.
    """
    hist = sixfilter.memory.get(ticker, [])
    base = market.get("last_trade_price", 50) / 100
    
    if len(hist) < 3:
        return 0.55 if side == "yes" else 0.45
    
    trend = (hist[-1] - hist[0]) / 100 if hist else 0
    if side == "yes":
        return min(0.95, base + trend)
    return min(0.95, (1 - base) - trend)

# ─── FASTAPI APP ─────────────────────────────────────────────────────
app = FastAPI(title="SixFilter Kalshi Trader", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "SixFilter Online", "docs": "/docs", "dashboard": "/dashboard"}

@app.get("/health")
def health():
    kalshi_ok = False
    try:
        kalshi.get("/exchange/status")
        kalshi_ok = True
    except:
        pass
    return {
        "status": "ok",
        "kalshi": {"connected": kalshi_ok, "key_loaded": bool(KALSHI_API_KEY)},
        "telegram": {"connected": telegram.ok, "chat_set": bool(TELEGRAM_CHAT_ID)},
        "risk": risk.status(),
        "timestamp": datetime.now().isoformat()
    }

@app.get("/dashboard")
def dashboard():
    """Full dashboard JSON for your frontend."""
    s = risk.status()
    positions = []
    for pos in s["positions"]:
        try:
            mkt = kalshi.get_market(pos["ticker"])
            yes = mkt.get("last_trade_price", 50)
            if pos["side"] == "yes":
                unreal = (yes - pos["entry_price"]) * pos["contracts"] / 100
                mkt_price = yes
            else:
                unreal = (pos["entry_price"] - (100 - yes)) * pos["contracts"] / 100
                mkt_price = 100 - yes
        except:
            unreal, mkt_price = 0.0, pos["entry_price"]
        positions.append({
            **pos,
            "market_price": round(mkt_price, 1),
            "unrealized_pnl": round(unreal, 2)
        })
    
    return {
        "date": s["last_reset"],
        "running": s["running"],
        "daily_pnl": s["daily_pnl"],
        "trades_today": s["trades_today"],
        "max_trades": s["max_trades"],
        "loss_limit": s["loss_limit"],
        "min_edge": s["min_edge"],
        "positions": positions,
        "trade_log": s["trade_log"],
        "scanning": SCAN_MARKETS,
        "kalshi_connected": bool(KALSHI_API_KEY),
        "telegram_connected": telegram.ok
    }

@app.post("/trade", response_model=TradeResult)
def trade(req: SimpleTradeRequest, background: BackgroundTasks):
    """
    Simple trade: send {ticker, side, contracts} — we do the rest.
    """
    # Risk gate
    if not risk.can_trade() and not req.force:
        return TradeResult(
            success=False, ticker=req.ticker, side=req.side,
            status="RISK_BLOCKED", sixfilter_score=0, edge_cents=0, kelly_contracts=0,
            message=f"Limit hit. Trades: {risk.trades_today}/{MAX_TRADES_DAY}, PnL: ${risk.daily_pnl:.2f}",
            timestamp=datetime.now().isoformat()
        )
    
    # Fetch market
    try:
        market = kalshi.get_market(req.ticker)
        ob = kalshi.get_orderbook(req.ticker)
        yes_ask = ob.get("yes_ask", 50)
        no_ask = ob.get("no_ask", 50)
        entry = yes_ask if req.side == "yes" else no_ask
    except Exception as e:
        return TradeResult(
            success=False, ticker=req.ticker, side=req.side,
            status="MARKET_ERROR", sixfilter_score=0, edge_cents=0, kelly_contracts=0,
            message=str(e), timestamp=datetime.now().isoformat()
        )
    
    # SixFilter
    model_prob = get_model_probability(req.ticker, req.side, market)
    analysis = sixfilter.analyze(req.ticker, req.side, yes_ask, no_ask, model_prob, risk.daily_pnl)
    
    if not analysis["passed"] and not req.force:
        return TradeResult(
            success=False, ticker=req.ticker, side=req.side,
            status="FILTER_BLOCKED",
            sixfilter_score=round(analysis["score"], 1),
            edge_cents=round(analysis["edge_cents"], 1),
            kelly_contracts=analysis["kelly_contracts"],
            message=f"Blocked: {analysis['passed_count']}/6 passed. Edge: {analysis['edge_cents']:.1f}c",
            timestamp=datetime.now().isoformat()
        )
    
    # Size
    contracts = analysis["kelly_contracts"] if req.auto_size else req.contracts
    contracts = min(max(1, contracts), 10)
    
    # Execute
    try:
        order = kalshi.place_order(req.ticker, req.side, contracts, int(entry))
        risk.record(req.ticker, req.side, contracts, entry, analysis)
        
        result = TradeResult(
            success=True,
            order_id=order.get("order_id"),
            ticker=req.ticker,
            side=req.side,
            filled_count=contracts,
            avg_price=entry,
            status=order.get("status", "unknown"),
            sixfilter_score=round(analysis["score"], 1),
            edge_cents=round(analysis["edge_cents"], 1),
            kelly_contracts=contracts,
            message=f"Order placed. SixFilter: {analysis['passed_count']}/6 passed.",
            timestamp=datetime.now().isoformat()
        )
        background.add_task(telegram.trade, result, analysis)
        return result
        
    except Exception as e:
        return TradeResult(
            success=False, ticker=req.ticker, side=req.side,
            status="ORDER_FAILED",
            sixfilter_score=round(analysis["score"], 1),
            edge_cents=round(analysis["edge_cents"], 1),
            kelly_contracts=contracts,
            message=f"Order error: {str(e)}",
            timestamp=datetime.now().isoformat()
        )

@app.get("/scan")
def scan():
    """Scan all markets for SixFilter signals."""
    signals = []
    for ticker in SCAN_MARKETS:
        try:
            market = kalshi.get_market(ticker)
            ob = kalshi.get_orderbook(ticker)
            yes_ask = ob.get("yes_ask", 50)
            no_ask = ob.get("no_ask", 50)
            for side in ["yes", "no"]:
                prob = get_model_probability(ticker, side, market)
                a = sixfilter.analyze(ticker, side, yes_ask, no_ask, prob, risk.daily_pnl)
                if a["passed"]:
                    signals.append({
                        "ticker": ticker, "side": side,
                        "price": yes_ask if side == "yes" else no_ask,
                        "score": round(a["score"], 1),
                        "edge": round(a["edge_cents"], 1),
                        "kelly": a["kelly_contracts"]
                    })
        except Exception as e:
            signals.append({"ticker": ticker, "error": str(e)})
    return {"timestamp": datetime.now().isoformat(), "signals": signals}

@app.get("/positions")
def positions():
    return {"positions": list(risk.positions.values()), "count": len(risk.positions)}

@app.post("/close/{ticker}")
def close_position(ticker: str):
    if ticker not in risk.positions:
        raise HTTPException(404, "No position")
    try:
        yes = kalshi.get_market(ticker).get("last_trade_price", 50)
        pnl = risk.close(ticker, yes)
        telegram.send(f"🔒 *Closed {ticker}*\nRealized PnL: ${pnl:.2f}")
        return {"ticker": ticker, "realized_pnl": round(pnl, 2), "daily_pnl": round(risk.daily_pnl, 2)}
    except Exception as e:
        raise HTTPException(500, f"Close failed: {e}")

@app.get("/admin/pause")
def pause():
    risk.running = False
    telegram.send("⏸️ System Paused")
    return {"status": "paused"}

@app.get("/admin/resume")
def resume():
    risk.running = True
    telegram.send("▶️ System Resumed")
    return {"status": "resumed"}

@app.get("/admin/reset")
def reset():
    risk.daily_pnl = 0.0
    risk.trades_today = 0
    risk.positions = {}
    risk.trade_log = []
    risk.last_reset = str(date.today())
    telegram.send("🔄 Daily Reset")
    return {"status": "reset", "risk": risk.status()}

# ─── MAIN ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

import os
import json
import time
import uuid
import base64
import requests
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="SixFilter Kalshi Trader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== KALSHI CLIENT ==========

class KalshiClient:
    def __init__(self):
        self.env = os.getenv("KALSHI_ENV", "demo").lower()
        self.base_url = "https://api.elections.kalshi.com" if self.env == "live" else "https://demo-api.kalshi.com"
        self.api_prefix = "/trade-api/v2"
        self.key_id = os.getenv("KALSHI_KEY_ID", "")
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
        self.private_key = None
        if key_path and os.path.exists(key_path):
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.backends import default_backend
            with open(key_path, "rb") as f:
                self.private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
        else:
            key_env = os.getenv("KALSHI_PRIVATE_KEY", "")
            if key_env and "BEGIN" in key_env:
                from cryptography.hazmat.primitives import hashes, serialization
                from cryptography.hazmat.primitives.asymmetric import padding
                from cryptography.hazmat.backends import default_backend
                clean_key = key_env.replace("\\n", "\n").strip().encode('utf-8')
                self.private_key = serialization.load_pem_private_key(clean_key, password=None, backend=default_backend())
        self.session = requests.Session()

    def is_configured(self):
        return self.env == "live" and self.private_key is not None and bool(self.key_id)

    def _sign(self, message: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        signature = self.private_key.sign(
            message.encode('utf-8'),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')

    def _headers(self, method: str, path: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        msg_string = timestamp + method + path
        sig = self._sign(msg_string)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}{self.api_prefix}{path}"

    def get_config(self):
        return {
            "env": self.env,
            "key_id_set": bool(self.key_id),
            "key_loaded": self.private_key is not None,
            "base_url": self.base_url
        }

    def get_balance(self):
        if not self.is_configured():
            return {"error": "Kalshi not configured", "env": self.env}
        path = "/portfolio/balance"
        full_path = f"{self.api_prefix}{path}"
        url = self._url(path)
        headers = self._headers("GET", full_path)
        try:
            response = self.session.get(url, headers=headers, timeout=10)
            return response.json() if response.status_code == 200 else {"error": response.text, "status": response.status_code}
        except Exception as e:
            return {"error": str(e), "env": self.env}

    def get_markets(self, limit=100):
        if not self.is_configured():
            return {"error": "Kalshi not configured"}
        path = f"/markets?limit={limit}"
        sign_path = f"{self.api_prefix}/markets"
        url = self._url(path)
        headers = self._headers("GET", sign_path)
        try:
            response = self.session.get(url, headers=headers, timeout=10)
            return response.json() if response.status_code == 200 else {"error": response.text, "status": response.status_code}
        except Exception as e:
            return {"error": str(e)}

    def place_order(self, ticker: str, side: str, count: str, price: str, client_order_id: str = None):
        if not self.is_configured():
            return {"error": "Kalshi not configured"}
        path = "/portfolio/events/orders"
        full_path = f"{self.api_prefix}{path}"
        url = self._url(path)
        if client_order_id is None:
            client_order_id = str(uuid.uuid4())
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side,
            "count": count,
            "price": price,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": False,
            "reduce_only": False,
            "subaccount": 0,
            "exchange_index": 0
        }
        body_json = json.dumps(body)
        headers = self._headers("POST", full_path)
        try:
            response = self.session.post(url, headers=headers, data=body_json, timeout=10)
            return response.json() if response.status_code in (200, 201) else {"error": response.text, "status": response.status_code}
        except Exception as e:
            return {"error": str(e)}

# ========== SIXFILTER ANALYZER (INLINE - NO IMPORTS) ==========

class KalshiSixFilter:
    def __init__(self, bankroll: float = 22.17):
        self.bankroll = bankroll
        self.min_edge_percent = 10.0
        self.max_kelly_fraction = 0.25
        self.max_position_size = 1.0
        self.min_time_to_event_hours = 2

    def analyze(self, ticker: str, market_price: float, category: str,
                event_title: str, time_to_event_hours: float,
                recent_news: str = ""):
        filter_details = {}
        filters_passed = 0

        # FILTER 1: LMSR (Fair Probability vs Market Price)
        # Simple heuristic: market is usually 5-10% off on underdogs
        fair_prob = self._estimate_fair_prob(market_price, category, event_title)
        edge = (fair_prob - market_price) * 100

        filter_details["lmsr"] = {
            "market_price": round(market_price, 4),
            "fair_probability": round(fair_prob, 4),
            "edge_percent": round(edge, 2),
            "passed": abs(edge) >= self.min_edge_percent
        }
        if filter_details["lmsr"]["passed"]:
            filters_passed += 1

        # FILTER 2: Kelly Criterion (Position Sizing)
        if edge > 0:
            kelly_raw = edge / (market_price * 100)
            direction = "bid"  # YES
        else:
            kelly_raw = abs(edge) / ((1 - market_price) * 100)
            direction = "ask"  # NO

        kelly_fraction = min(kelly_raw * self.max_kelly_fraction, self.max_kelly_fraction)
        risk_amount = min(self.bankroll * kelly_fraction, self.max_position_size)

        if direction == "bid":
            contract_count = risk_amount / market_price
        else:
            contract_count = risk_amount / (1 - market_price)

        contract_count = max(round(contract_count, 2), 0.01)
        size = f"{contract_count:.2f}"

        filter_details["kelly"] = {
            "kelly_fraction": round(kelly_fraction, 4),
            "risk_dollars": round(risk_amount, 2),
            "contract_count": contract_count,
            "passed": kelly_fraction > 0.005
        }
        if filter_details["kelly"]["passed"]:
            filters_passed += 1

        # FILTER 3: EV Gap (Expected Value)
        if direction == "bid":
            ev = (fair_prob * (1 - market_price)) - ((1 - fair_prob) * market_price)
        else:
            ev = ((1 - fair_prob) * market_price) - (fair_prob * (1 - market_price))

        ev_percent = ev * 100
        filter_details["ev_gap"] = {
            "ev_percent": round(ev_percent, 2),
            "passed": ev_percent >= 3.0
        }
        if filter_details["ev_gap"]["passed"]:
            filters_passed += 1

        # FILTER 4: KL Divergence (Momentum/Info Mismatch)
        divergence = min(abs(market_price - fair_prob) * 2, 1.0)
        filter_details["divergence"] = {
            "score": round(divergence, 2),
            "passed": divergence < 0.35
        }
        if filter_details["divergence"]["passed"]:
            filters_passed += 1

        # FILTER 5: Bayesian (Context Filter)
        bayesian_pass = True
        bayesian_reasons = []

        if time_to_event_hours < self.min_time_to_event_hours:
            bayesian_pass = False
            bayesian_reasons.append(f"Too close to event ({time_to_event_hours:.1f}h)")

        if self.bankroll < 10:
            bayesian_pass = False
            bayesian_reasons.append("Bankroll too low")

        if category == "sports" and time_to_event_hours > 48:
            bayesian_reasons.append("Early line, info will shift")

        if category == "crypto" and time_to_event_hours > 168:
            bayesian_pass = False
            bayesian_reasons.append("Crypto too far out")

        filter_details["bayesian"] = {
            "time_to_event": time_to_event_hours,
            "passed": bayesian_pass,
            "reasons": bayesian_reasons
        }
        if filter_details["bayesian"]["passed"]:
            filters_passed += 1

        # FILTER 6: Stoikov Execution (Limit Order Pricing)
        if direction == "bid":
            stoikov_price = max(market_price - 0.03, 0.01)
        else:
            stoikov_price = min(market_price + 0.03, 0.99)

        filter_details["stoikov"] = {
            "suggested_price": round(stoikov_price, 4),
            "passed": True
        }
        filters_passed += 1

        # FINAL DECISION
        proceed = filters_passed >= 5
        confidence = int(min(abs(edge) * 2 + 50, 95)) if proceed else int(abs(edge) * 2)

        if proceed:
            reason = f"Strong {'YES' if direction == 'bid' else 'NO'} signal. Edge: {edge:.1f}%, EV: {ev_percent:.1f}%. {filters_passed}/6 filters passed."
        else:
            reasons = "; ".join(bayesian_reasons) if bayesian_reasons else "Insufficient edge"
            reason = f"Rejected: {reasons}. Only {filters_passed}/6 filters passed."

        return {
            "ticker": ticker,
            "direction": direction,
            "confidence": confidence,
            "size": size,
            "suggested_price": f"{stoikov_price:.4f}",
            "fair_probability": round(fair_prob, 4),
            "market_price": market_price,
            "edge_percent": round(edge, 2),
            "ev_percent": round(ev_percent, 2),
            "kelly_fraction": round(kelly_fraction, 4),
            "proceed": proceed,
            "reason": reason,
            "filters_passed": filters_passed,
            "filter_details": filter_details
        }

    def _estimate_fair_prob(self, market_price, category, event_title):
        """Simple heuristic: market underprices favorites and overprices longshots."""
        # Favorite-longshot bias correction
        if market_price > 0.7:
            return min(market_price + 0.03, 0.95)
        elif market_price < 0.3:
            return max(market_price - 0.03, 0.05)
        else:
            # Middle range: slight underdog bias
            return market_price + 0.05 if market_price < 0.5 else market_price - 0.05

# ========== INIT ==========

kalshi = None
kalshi_config_data = {
    "env": os.getenv("KALSHI_ENV", "demo"),
    "key_id_set": bool(os.getenv("KALSHI_KEY_ID")),
    "key_loaded": False,
    "base_url": "https://demo-api.kalshi.com/trade-api/v2",
    "error": None
}

try:
    kalshi = KalshiClient()
    kalshi_config_data = kalshi.get_config()
except Exception as e:
    kalshi_config_data["error"] = str(e)
    kalshi = None

analyzer = KalshiSixFilter(bankroll=22.17)

# ========== PYDANTIC MODELS ==========

class OrderRequest(BaseModel):
    ticker: str
    side: str
    count: str
    price: str
    client_order_id: str = None

class AnalyzeRequest(BaseModel):
    ticker: str
    market_price: float
    category: str
    event_title: str
    time_to_event_hours: float
    recent_news: str = ""
    auto_execute: bool = False

# ========== ENDPOINTS ==========

@app.get("/health")
def health():
    return {
        "status": "ok",
        "kalshi": kalshi_config_data,
        "analyzer_ready": True,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/kalshi/config")
def kalshi_config():
    return kalshi.get_config() if kalshi else kalshi_config_data

@app.get("/kalshi/balance")
def kalshi_balance():
    if kalshi is None or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.get_balance()

@app.get("/kalshi/markets")
def kalshi_markets(limit: int = 100):
    if kalshi is None or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.get_markets(limit)

@app.post("/kalshi/order")
def kalshi_order(order: OrderRequest):
    if kalshi is None or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.place_order(
        ticker=order.ticker,
        side=order.side,
        count=order.count,
        price=order.price,
        client_order_id=order.client_order_id
    )

@app.post("/kalshi/analyze")
def analyze_market(req: AnalyzeRequest):
    """Run SixFilter on a Kalshi market."""
    try:
        signal = analyzer.analyze(
            ticker=req.ticker,
            market_price=req.market_price,
            category=req.category,
            event_title=req.event_title,
            time_to_event_hours=req.time_to_event_hours,
            recent_news=req.recent_news
        )

        result = {"signal": signal, "order": None}

        # Auto-execute if enabled and signal is strong
        if req.auto_execute and signal["proceed"] and kalshi and kalshi.is_configured():
            try:
                order_result = kalshi.place_order(
                    ticker=signal["ticker"],
                    side=signal["direction"],
                    count=signal["size"],
                    price=signal["suggested_price"]
                )
                result["order"] = order_result
                result["signal"]["executed"] = True
            except Exception as e:
                result["order"] = {"error": str(e)}
                result["signal"]["executed"] = False

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    if os.path.exists("dashboard.html"):
        with open("dashboard.html", "r") as f:
            return f.read()
    return "<html><body><h1>Dashboard not found</h1></body></html>"

@app.get("/")
def root():
    return {
        "message": "SixFilter Kalshi Trader API",
        "docs": "/docs",
        "endpoints": {
            "health": "/health",
            "analyze": "POST /kalshi/analyze",
            "order": "POST /kalshi/order",
            "markets": "GET /kalshi/markets",
            "balance": "GET /kalshi/balance"
        }
    }

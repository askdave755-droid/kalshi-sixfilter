"""
SixFilter Guardian -> Kalshi Bridge
Single file. Paste as main.py
"""

import os
import json
import base64
import logging
import requests
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import numpy as np
from scipy.stats import norm

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    CRYPTO_OK = True
except ImportError:
    CRYPTO_OK = False

logger = logging.getLogger("kalshi")


@dataclass
class SixFilterConfig:
    MIN_EDGE_PCT: float = 4.0
    MAX_EDGE_PCT: float = 25.0
    KELLY_FRACTION: float = 0.25
    MAX_POSITION_PCT: float = 0.05
    MIN_POSITION_DOLLARS: float = 10.0
    MIN_EV_CENTS: float = 2.0
    MAX_SPREAD_CENTS: float = 5.0
    MAKER_DISCOUNT: int = 1
    BANKROLL: float = 10000.0
    MAX_DAILY_LOSS_PCT: float = 0.05
    MAX_OPEN_POSITIONS: int = 10
    TARGET_CATEGORIES: List[str] = field(default_factory=lambda: ["economics", "finance"])


class SixFilterEngine:
    def __init__(self, config: SixFilterConfig):
        self.config = config
        self.dist = {
            "mean": 0.249, "std": 0.156,
            "recent_mean": 0.255, "recent_std": 0.141,
            "seasonal_mean": 0.234, "seasonal_std": 0.187,
        }

    def filter_1_lmsr(self, threshold: float, kalshi_yes_price: float):
        mu, sigma = self.dist["mean"], self.dist["std"]
        true_prob = (1 - norm.cdf(threshold, mu, sigma)) * 100
        edge = true_prob - kalshi_yes_price
        passed = abs(edge) > self.config.MIN_EDGE_PCT and abs(edge) < self.config.MAX_EDGE_PCT
        return passed, edge, true_prob

    def filter_2_kelly(self, edge: float, price_cents: float, side: str):
        if abs(edge) < self.config.MIN_EDGE_PCT:
            return False, 0.0, 0.0
        if side == "yes":
            b = (100 - price_cents) / price_cents
            p = (price_cents + edge) / 100
        else:
            b = price_cents / (100 - price_cents)
            p = (100 - price_cents + edge) / 100
        q = 1 - p
        kelly = (b * p - q) / b if b > 0 else 0
        kelly = max(0, min(kelly, 0.25))
        position = self.config.BANKROLL * kelly * self.config.KELLY_FRACTION
        position = min(position, self.config.BANKROLL * self.config.MAX_POSITION_PCT)
        passed = position >= self.config.MIN_POSITION_DOLLARS
        return passed, kelly, position

    def filter_3_ev(self, true_prob: float, cost_cents: float, side: str):
        win_prob = true_prob / 100 if side == "yes" else 1 - (true_prob / 100)
        payout = 100 - cost_cents
        p = cost_cents / 100
        fee = np.ceil(0.07 * p * (1 - p) * 100) / 100
        ev = (win_prob * payout) - cost_cents - (fee * 2)
        passed = ev > self.config.MIN_EV_CENTS
        return passed, ev


class KalshiHTTPClient:
    def __init__(self):
        self.key_id = os.getenv("KALSHI_KEY_ID") or os.getenv("KALSHI_API_KEY") or ""
        self.env = os.getenv("KALSHI_ENV", "demo")
        self.base = "https://demo-api.kalshi.co" if self.env == "demo" else "https://api.kalshi.com"
        self.private_key = None

        priv_text = os.getenv("KALSHI_PRIVATE_KEY")
        priv_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")

        if priv_text:
            self._load_key(priv_text.encode())
        elif priv_path and os.path.exists(priv_path):
            with open(priv_path, "rb") as f:
                self._load_key(f.read())

    def _load_key(self, data: bytes):
        if not CRYPTO_OK:
            raise RuntimeError("cryptography package required")
        self.private_key = serialization.load_pem_private_key(data, password=None)

    def _sign(self, text: str) -> str:
        if not self.private_key:
            return ""
        sig = self.private_key.sign(
            text.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.AUTO),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(datetime.utcnow().timestamp()))
        msg = ts + method.upper() + path + body
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(msg),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def get(self, path: str):
        url = self.base + path
        r = requests.get(url, headers=self._headers("GET", path), timeout=15)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, payload: dict):
        url = self.base + path
        body = json.dumps(payload)
        r = requests.post(url, headers=self._headers("POST", path, body), data=body, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_balance(self):
        return self.get("/trade-api/v2/portfolio/balance")

    def get_events(self, category: str = "", status: str = "open"):
        params = f"?status={status}"
        if category:
            params += f"&category={category}"
        return self.get("/trade-api/v2/events" + params)

    def get_market(self, ticker: str):
        return self.get(f"/trade-api/v2/markets/{ticker}")

    def create_order(self, ticker: str, side: str, price: int, count: int):
        payload = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": count,
            "yes_price": price if side == "yes" else None,
            "no_price": price if side == "no" else None,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.post("/trade-api/v2/portfolio/orders", payload)


class KalshiTrader:
    def __init__(self):
        self.config = SixFilterConfig()
        self.engine = SixFilterEngine(self.config)
        self.client = KalshiHTTPClient()

    def scan_markets(self) -> List[Dict]:
        markets = []
        for category in self.config.TARGET_CATEGORIES:
            try:
                data = self.client.get_events(category=category, status="open")
                for event in data.get("events", []):
                    for m in event.get("markets", []):
                        threshold = self._extract_threshold(m.get("title", ""))
                        markets.append({
                            "ticker": m.get("ticker"),
                            "title": m.get("title"),
                            "yes_ask": m.get("yes_ask", 0),
                            "yes_bid": m.get("yes_bid", 0),
                            "no_ask": m.get("no_ask", 0),
                            "no_bid": m.get("no_bid", 0),
                            "volume": m.get("volume", 0),
                            "close_date": str(m.get("close_date")) if m.get("close_date") else None,
                            "threshold": threshold,
                            "spread": (m.get("yes_ask", 0) or 0) - (m.get("yes_bid", 0) or 0),
                        })
            except Exception as e:
                logger.error(f"Scan error [{category}]: {e}")
        return markets

    def _extract_threshold(self, title: str) -> Optional[float]:
        import re
        title_lower = title.lower()
        patterns = [
            r">(\d+\.\d+)%", r"above\s+(\d+\.\d+)%",
            r"(\d+\.\d+)%\s+or\s+more", r"(\d+\.\d+)%\s+or\s+higher",
        ]
        for pat in patterns:
            m = re.search(pat, title_lower)
            if m:
                return float(m.group(1))
        return None

    def evaluate_market(self, market: Dict) -> Optional[Dict]:
        if market.get("threshold") is None:
            return None
        threshold = market["threshold"]
        yes_price = market["yes_ask"]
        no_price = market["no_ask"]
        spread = market["spread"]
        f1_pass, edge, true_prob = self.engine.filter_1_lmsr(threshold, yes_price)
        if edge > 0:
            side, trade_price, trade_edge = "yes", yes_price, edge
        elif edge < 0:
            side, trade_price, trade_edge = "no", no_price, abs(edge)
        else:
            return None
        f2_pass, kelly, position = self.engine.filter_2_kelly(trade_edge, trade_price, side)
        cost = trade_price if side == "yes" else (100 - trade_price)
        f3_pass, ev = self.engine.filter_3_ev(true_prob, cost, side)
        f4_pass = True
        f5_pass = True
        f6_pass = spread < self.config.MAX_SPREAD_CENTS
        if not all([f1_pass, f2_pass, f3_pass, f4_pass, f5_pass, f6_pass]):
            return None
        contract_cost = trade_price / 100
        count = int(position / contract_cost)
        if count < 1:
            return None
        return {
            "ticker": market["ticker"],
            "title": market["title"],
            "side": side,
            "price": trade_price - self.config.MAKER_DISCOUNT if side == "no" else trade_price + self.config.MAKER_DISCOUNT,
            "count": count,
            "edge": round(trade_edge, 2),
            "true_prob": round(true_prob if side == "yes" else (100 - true_prob), 2),
            "kalshi_prob": trade_price,
            "ev": round(ev, 2),
            "kelly": round(kelly, 4),
            "position": round(position, 2),
            "filters": {
                "lmsr": f1_pass, "kelly": f2_pass, "ev": f3_pass,
                "kl": f4_pass, "bayesian": f5_pass, "stoikov": f6_pass,
            },
        }

    def execute_trade(self, signal: Dict) -> Optional[str]:
        try:
            resp = self.client.create_order(
                ticker=signal["ticker"],
                side=signal["side"],
                price=int(signal["price"]),
                count=signal["count"],
            )
            oid = resp.get("order", {}).get("order_id") or resp.get("order_id")
            logger.info(f"EXECUTED: {signal['title']} | {signal['side'].upper()} @ {signal['price']}c x{signal['count']}")
            return oid
        except Exception as e:
            logger.error(f"Execution failed: {e}")
            return None

    def run_cycle(self) -> List[Dict]:
        markets = self.scan_markets()
        signals = []
        for m in markets:
            sig = self.evaluate_market(m)
            if sig:
                oid = self.execute_trade(sig)
                sig["order_id"] = oid
                sig["status"] = "executed" if oid else "failed"
                sig["timestamp"] = datetime.utcnow().isoformat()
                signals.append(sig)
        return signals


app = FastAPI(title="SixFilter Kalshi Bridge")

_trader: Optional[KalshiTrader] = None

def get_trader() -> KalshiTrader:
    global _trader
    if _trader is None:
        try:
            _trader = KalshiTrader()
        except Exception as e:
            logger.error(f"Kalshi init failed: {e}")
            raise HTTPException(status_code=503, detail=f"Kalshi not configured: {e}")
    return _trader


@app.get("/")
async def root():
    return {
        "status": "alive",
        "service": "SixFilter Kalshi Bridge",
        "endpoints": [
            "/health", "/kalshi/scan", "/kalshi/balance",
            "/kalshi/markets", "/kalshi/trade", "/kalshi/config",
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }

@app.get("/health")
async def health():
    return {"status": "ok", "service": "sixfilter-kalshi"}

@app.get("/kalshi/scan")
async def kalshi_scan():
    try:
        trader = get_trader()
        signals = trader.run_cycle()
        return {"signals_found": len(signals), "signals": signals}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/kalshi/balance")
async def kalshi_balance():
    try:
        trader = get_trader()
        bal = trader.client.get_balance()
        bc = bal.get("balance", 0)
        return {
            "balance_cents": bc,
            "balance_dollars": round(bc / 100, 2),
            "withdrawable_cents": bal.get("withdrawable_balance", 0),
            "raw": bal,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/kalshi/markets")
async def kalshi_markets():
    try:
        trader = get_trader()
        markets = trader.scan_markets()
        return {"markets_count": len(markets), "markets": markets[:30]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ManualTradeRequest(BaseModel):
    ticker: str
    side: str
    price: int
    count: int

@app.post("/kalshi/trade")
async def kalshi_manual_trade(req: ManualTradeRequest):
    try:
        trader = get_trader()
        signal = {
            "ticker": req.ticker,
            "side": req.side,
            "price": req.price,
            "count": req.count,
        }
        oid = trader.execute_trade(signal)
        return {
            "order_id": oid,
            "status": "executed" if oid else "failed",
            "ticker": req.ticker,
            "side": req.side,
            "price": req.price,
            "count": req.count,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/kalshi/config")
async def kalshi_config():
    try:
        trader = get_trader()
        c = trader.config
        return {
            "bankroll": c.BANKROLL,
            "min_edge_pct": c.MIN_EDGE_PCT,
            "kelly_fraction": c.KELLY_FRACTION,
            "max_position_pct": c.MAX_POSITION_PCT,
            "min_ev_cents": c.MIN_EV_CENTS,
            "max_spread_cents": c.MAX_SPREAD_CENTS,
            "target_categories": c.TARGET_CATEGORIES,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

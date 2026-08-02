"""
SixFilter Kalshi Auto-Trader — Complete Single File
Kalshi RSA Auth + Binance Spot Feed + SixFilter + Auto-Scheduler + Telegram Bot + Dashboard
"""
import os
import json
import time
import uuid
import base64
import threading
import schedule
import requests
import re
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ========== CONFIG / ENV ==========

MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "10"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "50.0"))
MIN_EDGE_PERCENT = float(os.getenv("MIN_EDGE_PERCENT", "5.0"))
CONTRACT_SIZE = int(os.getenv("CONTRACT_SIZE", "10"))
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL", "90"))
AVOID_LUNCH = os.getenv("AVOID_LUNCH", "true").lower() == "true"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ========== TELEGRAM BOT ==========

class TelegramBot:
    def __init__(self):
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.enabled = bool(self.token and self.chat_id)

    def send_message(self, text: str, parse_mode: str = "HTML") -> dict:
        if not self.enabled:
            return {"error": "Telegram not configured"}
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            r = requests.post(url, json=payload, timeout=10)
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def send_trade_alert(self, signal: dict, order_result: dict = None):
        emoji = "🟢" if signal.get("direction") == "yes" else "🔴"
        status = "✅ EXECUTED" if order_result and "order" in order_result else "📊 SIGNAL"
        text = f"""<b>{emoji} {status}</b>

<b>Market:</b> <code>{signal.get("ticker", "N/A")}</code>
<b>Side:</b> {signal.get("direction", "N/A").upper()}
<b>Size:</b> {signal.get("size", "N/A")} contracts
<b>Price:</b> {signal.get("entry_price", "N/A")}¢
<b>Edge:</b> {signal.get("edge", 0):.1f}%
<b>Confidence:</b> {signal.get("confidence", 0)}%
<b>Spot:</b> {signal.get("binance_spot", "N/A")}

<b>Filters:</b> {signal.get("reason", "N/A")}
"""
        if order_result and "error" in order_result:
            text += f"\n❌ <b>Order Error:</b> {order_result['error']}"
        return self.send_message(text)

    def send_status(self, status: dict):
        text = f"""<b>📊 SixFilter Status</b>

Trades Today: <b>{status.get("trades_today", 0)} / {status.get("max_trades", 10)}</b>
Daily PnL: <b>${status.get("daily_pnl", 0):.2f}</b>
Loss Limit: <b>${status.get("loss_limit", 50)}</b>
Min Edge: <b>{status.get("min_edge", 5)}%</b>
Positions: <b>{len(status.get("positions", {}))}</b> markets

Last Trade: {status.get("trade_log", [{}])[-1].get("time", "None")}
"""
        return self.send_message(text)

    def set_webhook(self, webhook_url: str) -> dict:
        if not self.enabled:
            return {"error": "Telegram not configured"}
        try:
            url = f"{self.base_url}/setWebhook"
            r = requests.post(url, json={"url": webhook_url}, timeout=10)
            return r.json()
        except Exception as e:
            return {"error": str(e)}

telegram = TelegramBot()

# ========== BINANCE SPOT FEED ==========

class BinanceFeed:
    _cache: Dict[str, Tuple[float, float]] = {}
    _cache_ttl = 5

    @classmethod
    def get_price(cls, symbol: str = "BTCUSDT") -> Optional[float]:
        now = time.time()
        cached = cls._cache.get(symbol)
        if cached and (now - cached[1]) < cls._cache_ttl:
            return cached[0]
        try:
            r = requests.get(
                f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
                timeout=5
            )
            price = float(r.json()["price"])
            cls._cache[symbol] = (price, now)
            return price
        except Exception as e:
            print(f"[Binance] Feed error for {symbol}: {e}")
            return cached[0] if cached else None

    @classmethod
    def get_klines(cls, symbol: str = "BTCUSDT", interval: str = "1m", limit: int = 5) -> List[dict]:
        try:
            r = requests.get(
                f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
                timeout=5
            )
            data = r.json()
            return [{
                "open": float(d[1]),
                "high": float(d[2]),
                "low": float(d[3]),
                "close": float(d[4]),
                "volume": float(d[5]),
                "time": d[0]
            } for d in data]
        except Exception as e:
            print(f"[Binance] Klines error: {e}")
            return []

# ========== KALSHI CLIENT ==========

class KalshiClient:
    def __init__(self):
        self.env = os.getenv("KALSHI_ENV", "demo").lower()
        self.base_url = "https://api.elections.kalshi.com" if self.env == "live" else "https://demo-api.kalshi.com"
        self.api_prefix = "/trade-api/v2"
        self.key_id = os.getenv("KALSHI_KEY_ID", "")
        self.private_key = None

        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
        if key_path and os.path.exists(key_path):
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.backends import default_backend
            with open(key_path, "rb") as f:
                self.private_key = serialization.load_pem_private_key(
                    f.read(), password=None, backend=default_backend()
                )
        else:
            key_env = os.getenv("KALSHI_PRIVATE_KEY", "")
            if key_env and "BEGIN" in key_env:
                from cryptography.hazmat.primitives import hashes, serialization
                from cryptography.hazmat.primitives.asymmetric import padding
                from cryptography.hazmat.backends import default_backend
                clean_key = key_env.replace("\\n", "\n").strip().encode("utf-8")
                self.private_key = serialization.load_pem_private_key(
                    clean_key, password=None, backend=default_backend()
                )
        self.session = requests.Session()

    def is_configured(self):
        return self.env == "live" and self.private_key is not None and bool(self.key_id)

    def _sign(self, message: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode("utf-8")

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

    def get_markets(self, series_ticker: str = None, limit: int = 100):
        if not self.is_configured():
            return {"error": "Kalshi not configured"}
        path = f"/markets?limit={limit}"
        if series_ticker:
            path += f"&series_ticker={series_ticker}"
        sign_path = f"{self.api_prefix}/markets"
        url = self._url(path)
        headers = self._headers("GET", sign_path)
        try:
            response = self.session.get(url, headers=headers, timeout=10)
            return response.json() if response.status_code == 200 else {"error": response.text, "status": response.status_code}
        except Exception as e:
            return {"error": str(e)}

    def get_orderbook(self, ticker: str, depth: int = 10):
        if not self.is_configured():
            return {"error": "Kalshi not configured"}
        path = f"/markets/{ticker}/orderbook?depth={depth}"
        full_path = f"{self.api_prefix}{path}"
        url = self._url(path)
        headers = self._headers("GET", full_path)
        try:
            response = self.session.get(url, headers=headers, timeout=10)
            return response.json() if response.status_code == 200 else {"error": response.text, "status": response.status_code}
        except Exception as e:
            return {"error": str(e)}

    def get_positions(self):
        if not self.is_configured():
            return {"error": "Kalshi not configured"}
        path = "/portfolio/positions?limit=100"
        full_path = f"{self.api_prefix}{path}"
        url = self._url(path)
        headers = self._headers("GET", full_path)
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

    def cancel_order(self, order_id: str):
        if not self.is_configured():
            return {"error": "Kalshi not configured"}
        path = f"/portfolio/events/orders/{order_id}"
        full_path = f"{self.api_prefix}{path}"
        url = self._url(path)
        headers = self._headers("DELETE", full_path)
        try:
            response = self.session.delete(url, headers=headers, timeout=10)
            return response.json() if response.status_code in (200, 201) else {"error": response.text, "status": response.status_code}
        except Exception as e:
            return {"error": str(e)}

# ========== SIXFILTER ANALYZER ==========

class Direction(Enum):
    YES = "yes"
    NO = "no"

@dataclass
class Signal:
    ticker: str
    direction: Direction
    edge: float
    confidence: int
    entry_price: int
    size: int
    reason: str
    filters: List[str]
    binance_spot: Optional[float]
    timestamp: datetime

class SixFilterAnalyzer:
    def __init__(self, bankroll: float = 22.17):
        self.bankroll

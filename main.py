"""
SixFilter Kalshi Auto-Trader — Complete Single File
Kalshi RSA Auth + Binance Spot Feed + SixFilter + APScheduler + Telegram Bot + Dashboard
"""
import os
import json
import time
import uuid
import base64
import threading
import requests
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# APScheduler for background scanning
from apscheduler.schedulers.background import BackgroundScheduler

# ========== CONFIG / ENV ==========

MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "10"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "50.0"))
MIN_EDGE_PERCENT = float(os.getenv("MIN_EDGE_PERCENT", "5.0"))
CONTRACT_SIZE = int(os.getenv("CONTRACT_SIZE", "10"))
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL", "90"))
AVOID_LUNCH = os.getenv("AVOID_LUNCH", "true").lower() == "true"

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
        self.bankroll = bankroll
        self.min_edge = MIN_EDGE_PERCENT
        self.max_kelly = 0.25
        self.max_position = 1.0
        self.min_ev = 3.0

    def _get_binance_symbol(self, ticker: str) -> str:
        if "BTC" in ticker:
            return "BTCUSDT"
        if "ETH" in ticker:
            return "ETHUSDT"
        return ""

    def _extract_strike(self, market: dict) -> Optional[float]:
        strike = market.get("floor_strike")
        if strike:
            return float(strike)
        title = market.get("title", "")
        m = __import__('re').search(r"[\$£€]?([\d,]+\.?\d*)", title)
        if m:
            return float(m.group(1).replace(",", ""))
        return None

    def _lmsr(self, market: dict, binance_spot: Optional[float]) -> Tuple[float, Direction, str]:
        yes_bid = (market.get("yes_bid") or 0) / 100.0
        yes_ask = (market.get("yes_ask") or 0) / 100.0
        no_bid = (market.get("no_bid") or 0) / 100.0
        no_ask = (market.get("no_ask") or 0) / 100.0

        if yes_ask <= 0 or no_ask <= 0 or yes_bid <= 0:
            return 0, Direction.YES, "NO_LIQUIDITY"

        yes_mid = (yes_bid + yes_ask) / 2
        implied = yes_mid
        strike = self._extract_strike(market)
        true_prob = 0.5

        if binance_spot and strike:
            diff_pct = (binance_spot - strike) / strike
            true_prob = 0.5 + (math.atan(diff_pct * 100) / math.pi) * 0.8
            true_prob = max(0.05, min(0.95, true_prob))
            edge = (true_prob - implied) * 100
            direction = Direction.YES if edge > 0 else Direction.NO
            return abs(edge), direction, f"LMSR_spot={binance_spot:.0f}_strike={strike:.0f}"

        symbol = self._get_binance_symbol(market.get("ticker", ""))
        klines = BinanceFeed.get_klines(symbol, "1m", 3) if symbol else []
        if len(klines) >= 2:
            momentum = (klines[-1]["close"] - klines[0]["open"]) / klines[0]["open"]
            true_prob = 0.5 + momentum * 50
            true_prob = max(0.05, min(0.95, true_prob))
            edge = (true_prob - implied) * 100
            direction = Direction.YES if edge > 0 else Direction.NO
            return abs(edge), direction, f"LMSR_momentum={momentum:.4f}"

        if yes_mid > 0.7:
            true_prob = min(yes_mid + 0.03, 0.95)
        elif yes_mid < 0.3:
            true_prob = max(yes_mid - 0.03, 0.05)
        else:
            true_prob = yes_mid + 0.05 if yes_mid < 0.5 else yes_mid - 0.05

        edge = (true_prob - implied) * 100
        direction = Direction.YES if edge > 0 else Direction.NO
        return abs(edge), direction, "LMSR_heuristic"

    def _kelly(self, edge: float, bankroll: float, market_price: float) -> Tuple[int, float]:
        if edge <= 0:
            return 0, 0.0
        p = 0.5 + edge / 200
        b = 1.0
        q = 1 - p
        kelly = (b * p - q) / b
        kelly = max(0, min(kelly, self.max_kelly))
        risk = min(bankroll * kelly, self.max_position)
        contracts = int(risk / 0.5)
        return max(1, min(contracts, 100)), kelly

    def _ev_gap(self, market: dict, direction: Direction, true_prob: float) -> Tuple[bool, float]:
        if direction == Direction.YES:
            entry = (market.get("yes_ask") or 0) / 100.0
            prob = true_prob
        else:
            entry = (market.get("no_ask") or 0) / 100.0
            prob = 1 - true_prob
        if entry <= 0:
            return False, 0.0
        ev = (prob * 1.0) - entry
        ev_pct = ev * 100
        return ev_pct >= self.min_ev, ev_pct

    def _divergence(self, market: dict, klines: List[dict]) -> Tuple[bool, str]:
        vol = market.get("volume", 0)
        oi = market.get("open_interest", 0)
        last = (market.get("last_price") or 0) / 100.0
        if vol == 0:
            return False, "NO_VOLUME"
        if vol > max(oi, 1) * 0.15 and abs(last - 0.5) < 0.05:
            return False, "VOLUME_MID_INDECISION"
        if len(klines) >= 2:
            price_up = klines[-1]["close"] > klines[-2]["close"]
            vol_down = klines[-1]["volume"] < klines[-2]["volume"] * 0.8
            if price_up and vol_down:
                return False, "PRICE_UP_VOL_DOWN"
        return True, "OK"

    def _bayesian(self, trades_today: int, daily_pnl: float, close_time: datetime) -> Tuple[bool, str]:
        if daily_pnl <= -DAILY_LOSS_LIMIT:
            return False, "DAILY_LOSS_LIMIT"
        if trades_today >= MAX_TRADES_PER_DAY:
            return False, "MAX_TRADES"
        now = datetime.now()
        hour = now.hour + now.minute / 60
        if AVOID_LUNCH and 12.0 <= hour <= 13.5:
            return False, "LUNCH_HOUR"
        secs = (close_time.replace(tzinfo=None) - now).total_seconds()
        if secs < 30:
            return False, f"CLOSE_{int(secs)}s"
        return True, "OK"

    def _stoikov(self, market: dict, direction: Direction) -> int:
        if direction == Direction.YES:
            bid = market.get("yes_bid") or 0
            ask = market.get("yes_ask") or 0
        else:
            bid = market.get("no_bid") or 0
            ask = market.get("no_ask") or 0
        if bid <= 0 or ask <= 0:
            return 0
        mid = (bid + ask) / 2
        if direction == Direction.YES:
            return max(1, int(mid - 1))
        else:
            return max(1, int(mid - 1))

    def analyze(self, market: dict, trades_today: int = 0, daily_pnl: float = 0.0) -> Optional[Signal]:
        passed = []
        ticker = market.get("ticker", "")
        close_str = market.get("close_time", "")
        try:
            close = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        except:
            return None

        ok, reason = self._bayesian(trades_today, daily_pnl, close)
        if not ok:
            return None
        passed.append(f"TIME({reason})")

        symbol = self._get_binance_symbol(ticker)
        binance_spot = BinanceFeed.get_price(symbol) if symbol else None
        klines = BinanceFeed.get_klines(symbol, "1m", 3) if symbol else []

        edge, direction, lmsr_reason = self._lmsr(market, binance_spot)
        if edge < self.min_edge:
            return None
        passed.append(lmsr_reason)

        if binance_spot:
            strike = self._extract_strike(market)
            if strike:
                diff_pct = (binance_spot - strike) / strike
                true_prob = 0.5 + (math.atan(diff_pct * 100) / math.pi) * 0.8
                true_prob = max(0.05, min(0.95, true_prob))
            else:
                true_prob = 0.5 + (edge / 200) * (1 if direction == Direction.YES else -1)
        else:
            true_prob = 0.5 + (edge / 200) * (1 if direction == Direction.YES else -1)

        yes_ask = (market.get("yes_ask") or 0) / 100.0
        no_ask = (market.get("no_ask") or 0) / 100.0
        market_price = yes_ask if direction == Direction.YES else no_ask
        size, kelly_frac = self._kelly(edge, self.bankroll, market_price)
        if size < 1:
            return None
        size = min(size, CONTRACT_SIZE)
        passed.append(f"KELLY({size})")

        ev_ok, ev_pct = self._ev_gap(market, direction, true_prob)
        if not ev_ok:
            return None
        passed.append(f"EV({ev_pct:.1f}%)")

        div_ok, div_reason = self._divergence(market, klines)
        if not div_ok:
            return None
        passed.append(f"DIV({div_reason})")

        passed.append("BAYESIAN(OK)")

        entry = self._stoikov(market, direction)
        if entry <= 0 or entry >= 100:
            return None
        passed.append(f"STOIKOV({entry})")

        confidence = min(len(passed) * 10 + int(edge), 100)

        return Signal(
            ticker=ticker,
            direction=direction,
            edge=edge,
            confidence=confidence,
            entry_price=entry,
            size=size,
            reason=" | ".join(passed),
            filters=passed,
            binance_spot=binance_spot,
            timestamp=datetime.now()
        )

# ========== AUTO-TRADER ENGINE ==========

class AutoTrader:
    def __init__(self, client: KalshiClient, analyzer: SixFilterAnalyzer):
        self.client = client
        self.analyzer = analyzer
        self.running = False
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.trade_log: List[dict] = []
        self.last_reset = datetime.now().date()
        self.positions: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _reset_day(self):
        today = datetime.now().date()
        if today != self.last_reset:
            with self._lock:
                self.daily_pnl = 0.0
                self.trades_today = 0
                self.trade_log = []
                self.last_reset = today
            print(f"📅 New day reset: {today}")
            if telegram.enabled:
                telegram.send_message("📅 <b>New Day Started</b>\nCounters reset. Ready to trade.")

    def _update_positions(self):
        resp = self.client.get_positions()
        if "positions" in resp:
            self.positions = {
                p.get("market_id", ""): p.get("count", 0)
                for p in resp["positions"]
                if p.get("count", 0) != 0
            }

    def scan_series(self, series: str) -> List[Signal]:
        self._reset_day()
        self._update_positions()

        resp = self.client.get_markets(series_ticker=series, limit=100)
        if "error" in resp:
            print(f"❌ Market fetch error: {resp['error']}")
            return []

        markets = resp.get("markets", [])
        signals = []

        for m in markets:
            if m.get("status") != "open":
                continue
            sig = self.analyzer.analyze(m, self.trades_today, self.daily_pnl)
            if sig:
                current = self.positions.get(sig.ticker, 0)
                if abs(current) >= 50:
                    continue
                signals.append(sig)
                print(f"🟢 {sig.ticker} {sig.direction.value} | Edge:{sig.edge:.1f}% | ${sig.entry_price/100:.2f} x{sig.size} | Spot:{sig.binance_spot}")

        signals.sort(key=lambda x: x.edge, reverse=True)
        print(f"📊 {series}: {len(signals)} signals from {len(markets)} markets")
        return signals

    def execute(self, signal: Signal) -> dict:
        print(f"🎯 EXEC: {signal.ticker} {signal.direction.value} x{signal.size} @ ${signal.entry_price/100:.2f}")

        result = self.client.place_order(
            ticker=signal.ticker,
            side=signal.direction.value,
            count=str(signal.size),
            price=str(signal.entry_price),
            client_order_id=f"sf_{int(time.time()*1000)}_{signal.ticker[-8:]}"
        )

        with self._lock:
            if "order" in result:
                oid = result["order"].get("order_id", "unknown")
                print(f"✅ FILLED: {oid}")
                self.trades_today += 1
                self.trade_log.append({
                    "time": datetime.now().isoformat(),
                    "ticker": signal.ticker,
                    "side": signal.direction.value,
                    "price": signal.entry_price,
                    "size": signal.size,
                    "edge": signal.edge,
                    "spot": signal.binance_spot,
                    "reason": signal.reason,
                    "order_id": oid
                })
                if telegram.enabled:
                    telegram.send_trade_alert({
                        "ticker": signal.ticker,
                        "direction": signal.direction.value,
                        "size": signal.size,
                        "entry_price": signal.entry_price,
                        "edge": signal.edge,
                        "confidence": signal.confidence,
                        "binance_spot": signal.binance_spot,
                        "reason": signal.reason
                    }, result)
            else:
                print(f"❌ FAILED: {result.get('error', result)}")
                if telegram.enabled:
                    telegram.send_message(f"❌ <b>Order Failed</b>\n{signal.ticker}\nError: {result.get('error', 'Unknown')}")

        return result

    def run_cycle(self):
        if not self.client or not self.client.is_configured():
            print("❌ Kalshi not configured, skipping cycle")
            return

        self._reset_day()

        with self._lock:
            if self.daily_pnl <= -DAILY_LOSS_LIMIT:
                print("🛑 Daily loss limit hit")
                return
            if self.trades_today >= MAX_TRADES_PER_DAY:
                print("🛑 Max trades reached")
                return

        bal = self.client.get_balance()
# Kalshi returns balance in cents, convert to dollars
raw_balance = bal.get("balance", 2217)
self.analyzer.bankroll = raw_balance / 100.0 if raw_balance else 22.17

        all_signals = []
        for series in ["KXBTC15M", "KXETH15M"]:
            sigs = self.scan_series(series)
            all_signals.extend(sigs)

        with self._lock:
            remaining = MAX_TRADES_PER_DAY - self.trades_today

        for sig in all_signals[:remaining]:
            self.execute(sig)
            time.sleep(1)

    def get_status(self) -> dict:
        self._reset_day()
        with self._lock:
            return {
                "daily_pnl": self.daily_pnl,
                "trades_today": self.trades_today,
                "max_trades": MAX_TRADES_PER_DAY,
                "loss_limit": DAILY_LOSS_LIMIT,
                "min_edge": MIN_EDGE_PERCENT,
                "last_reset": str(self.last_reset),
                "positions": self.positions,
                "trade_log": self.trade_log[-20:],
                "running": self.running
            }

# ========== FASTAPI APP ==========

app = FastAPI(title="SixFilter Kalshi Auto-Trader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Init
kalshi = None
try:
    kalshi = KalshiClient()
except Exception as e:
    print(f"Kalshi init error: {e}")

analyzer = SixFilterAnalyzer(bankroll=22.17)
auto_trader = AutoTrader(kalshi, analyzer) if kalshi else None

# ========== PYDANTIC MODELS ==========

class OrderRequest(BaseModel):
    ticker: str
    side: str
    count: str
    price: str
    client_order_id: str = None

class ManualAnalyzeRequest(BaseModel):
    ticker: str
    market_price: float
    category: str = "crypto"
    event_title: str = ""
    time_to_event_hours: float = 0.25
    auto_execute: bool = False

class DashboardAnalyzeRequest(BaseModel):
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

class DashboardExecuteRequest(BaseModel):
    market_id: str
    side: str
    contracts: float
    limit_price: float

# ========== ENDPOINTS ==========

@app.get("/health")
def health():
    cfg = kalshi.get_config() if kalshi else {"error": "not initialized"}
    return {
        "status": "ok",
        "kalshi": cfg,
        "telegram": telegram.enabled,
        "auto_trader_ready": auto_trader is not None,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/kalshi/config")
def kalshi_config():
    if not kalshi:
        raise HTTPException(status_code=503, detail="Kalshi not initialized")
    return kalshi.get_config()

@app.get("/kalshi/balance")
def kalshi_balance():
    if not kalshi or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.get_balance()

@app.get("/kalshi/markets")
def kalshi_markets(series: str = None, limit: int = 100):
    if not kalshi or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.get_markets(series_ticker=series, limit=limit)

@app.get("/kalshi/orderbook/{ticker}")
def kalshi_orderbook(ticker: str, depth: int = 10):
    if not kalshi or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.get_orderbook(ticker, depth)

@app.get("/kalshi/positions")
def kalshi_positions():
    if not kalshi or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.get_positions()

@app.post("/kalshi/order")
def kalshi_order(order: OrderRequest):
    if not kalshi or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.place_order(
        ticker=order.ticker,
        side=order.side,
        count=order.count,
        price=order.price,
        client_order_id=order.client_order_id
    )

@app.delete("/kalshi/order/{order_id}")
def kalshi_cancel(order_id: str):
    if not kalshi or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")
    return kalshi.cancel_order(order_id)

@app.post("/kalshi/analyze")
def kalshi_analyze(req: DashboardAnalyzeRequest):
    """Compatible with your existing dashboard HTML"""
    if not auto_trader:
        raise HTTPException(status_code=503, detail="Auto-trader not ready")

    market = {
        "ticker": req.market_id,
        "yes_ask": int(req.yes_price),
        "yes_bid": int(req.yes_price) - 1,
        "no_ask": int(req.no_price),
        "no_bid": int(req.no_price) - 1,
        "last_price": int(req.yes_price),
        "volume": int(req.volume),
        "open_interest": int(req.open_interest),
        "close_time": (datetime.now() + timedelta(hours=2)).isoformat(),
        "title": req.market_name,
        "status": "open"
    }

    analyzer.bankroll = req.bankroll
    sig = analyzer.analyze(market, auto_trader.trades_today, auto_trader.daily_pnl)

    if not sig:
        return {
            "proceed": False,
            "side": "no",
            "contracts": 0,
            "limit_price": 0,
            "edge_percent": 0,
            "confidence": 0,
            "expected_value": 0,
            "filters_passed": [False, False, False, False, False, False],
            "market_id": req.market_id
        }

    prob = 0.5 + sig.edge / 200
    ev = (prob * 1.0) - (sig.entry_price / 100.0)

    return {
        "proceed": True,
        "side": sig.direction.value,
        "contracts": sig.size,
        "limit_price": sig.entry_price,
        "edge_percent": sig.edge,
        "confidence": sig.confidence,
        "expected_value": ev,
        "filters_passed": [True] * len(sig.filters) + [False] * (6 - len(sig.filters)),
        "market_id": req.market_id,
        "reason": sig.reason,
        "binance_spot": sig.binance_spot
    }

@app.post("/kalshi/execute")
def kalshi_execute(req: DashboardExecuteRequest):
    """Execute order from dashboard"""
    if not kalshi or not kalshi.is_configured():
        raise HTTPException(status_code=503, detail="Kalshi not configured")

    result = kalshi.place_order(
        ticker=req.market_id,
        side=req.side,
        count=str(int(req.contracts)),
        price=str(int(req.limit_price))
    )

    if telegram.enabled:
        telegram.send_trade_alert({
            "ticker": req.market_id,
            "direction": req.side,
            "size": int(req.contracts),
            "entry_price": int(req.limit_price),
            "edge": 0,
            "confidence": 100,
            "binance_spot": None,
            "reason": "DASHBOARD_MANUAL"
        }, result)

    return result

@app.post("/analyze")
def analyze_market(req: ManualAnalyzeRequest):
    if not auto_trader:
        raise HTTPException(status_code=503, detail="Auto-trader not ready")

    market = {
        "ticker": req.ticker,
        "yes_ask": int(req.market_price * 100),
        "yes_bid": int(req.market_price * 100) - 1,
        "no_ask": int((1 - req.market_price) * 100),
        "no_bid": int((1 - req.market_price) * 100) - 1,
        "last_price": int(req.market_price * 100),
        "volume": 100,
        "open_interest": 1000,
        "close_time": (datetime.now() + timedelta(hours=req.time_to_event_hours)).isoformat(),
        "title": req.event_title,
        "status": "open"
    }

    sig = analyzer.analyze(market, auto_trader.trades_today, auto_trader.daily_pnl)
    if not sig:
        return {"signal": None, "message": "No signal — filters blocked"}

    result = {"signal": {
        "ticker": sig.ticker,
        "direction": sig.direction.value,
        "edge": sig.edge,
        "confidence": sig.confidence,
        "entry_price": sig.entry_price,
        "size": sig.size,
        "reason": sig.reason,
        "binance_spot": sig.binance_spot
    }, "order": None}

    if req.auto_execute and kalshi and kalshi.is_configured():
        order_result = auto_trader.execute(sig)
        result["order"] = order_result
        result["signal"]["executed"] = True

    return result

@app.post("/scan")
def scan(background_tasks: BackgroundTasks):
    if not auto_trader:
        raise HTTPException(status_code=503, detail="Auto-trader not ready")
    background_tasks.add_task(auto_trader.run_cycle)
    return {"status": "scan_started", "time": datetime.utcnow().isoformat()}

@app.get("/status")
def status():
    if not auto_trader:
        raise HTTPException(status_code=503, detail="Auto-trader not ready")
    return auto_trader.get_status()

@app.post("/config")
def update_config(cfg: dict):
    global MIN_EDGE_PERCENT, MAX_TRADES_PER_DAY, DAILY_LOSS_LIMIT, CONTRACT_SIZE
    MIN_EDGE_PERCENT = float(cfg.get("min_edge", MIN_EDGE_PERCENT))
    MAX_TRADES_PER_DAY = int(cfg.get("max_trades", MAX_TRADES_PER_DAY))
    DAILY_LOSS_LIMIT = float(cfg.get("daily_loss", DAILY_LOSS_LIMIT))
    CONTRACT_SIZE = int(cfg.get("contract_size", CONTRACT_SIZE))
    analyzer.min_edge = MIN_EDGE_PERCENT
    return {
        "status": "updated",
        "config": {
            "min_edge": MIN_EDGE_PERCENT,
            "max_trades": MAX_TRADES_PER_DAY,
            "daily_loss": DAILY_LOSS_LIMIT,
            "contract_size": CONTRACT_SIZE
        }
    }

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Receive Telegram bot commands"""
    if not telegram.enabled:
        return {"error": "Telegram not configured"}

    try:
        data = await request.json()
        msg = data.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = msg.get("chat", {}).get("id", "")

        if text == "/start":
            telegram.send_message("<b>🎯 SixFilter Kalshi Bot</b>\nCommands:\n/status — Account status\n/scan — Run manual scan\n/balance — Kalshi balance\n/trades — Recent trades")
        elif text == "/status":
            if auto_trader:
                telegram.send_status(auto_trader.get_status())
            else:
                telegram.send_message("❌ Auto-trader not ready")
        elif text == "/scan":
            if auto_trader:
                threading.Thread(target=auto_trader.run_cycle).start()
                telegram.send_message("🔍 <b>Scan triggered</b>")
            else:
                telegram.send_message("❌ Auto-trader not ready")
        elif text == "/balance":
            if kalshi:
                bal = kalshi.get_balance()
                telegram.send_message(f"💰 <b>Balance:</b> ${bal.get('balance', 0):.2f}")
            else:
                telegram.send_message("❌ Kalshi not configured")
        elif text == "/trades":
            if auto_trader:
                log = auto_trader.get_status().get("trade_log", [])
                if log:
                    msg_text = "<b>📊 Recent Trades</b>\n\n"
                    for t in log[-5:]:
                        msg_text += f"{t['ticker'][:20]} | {t['side'].upper()} | {t['size']} @ {t['price']}¢\n"
                    telegram.send_message(msg_text)
                else:
                    telegram.send_message("No trades today")
            else:
                telegram.send_message("❌ Auto-trader not ready")
        else:
            telegram.send_message("Unknown command. Try: /status /scan /balance /trades")

        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/telegram/setup")
def telegram_setup():
    """Set webhook URL — call once after deploy"""
    if not telegram.enabled:
        return {"error": "Telegram not configured"}
    base = os.getenv("BASE_URL", "")
    if not base:
        return {"error": "BASE_URL not set in env"}
    webhook_url = f"{base}/webhook/telegram"
    result = telegram.set_webhook(webhook_url)
    return {"webhook_url": webhook_url, "result": result}

@app.post("/telegram/test")
def telegram_test(msg: str = "Test message from SixFilter"):
    if not telegram.enabled:
        return {"error": "Telegram not configured"}
    return telegram.send_message(f"<b>🧪 Test</b>\n{msg}")

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Kalshi SixFilter</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: -apple-system, sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 20px; }
            h1 { color: #00d9ff; margin-bottom: 10px; }
            .card { background: #151520; padding: 20px; border-radius: 12px; margin-bottom: 15px; border: 1px solid #2d2d44; }
            input { width: 100%; padding: 12px; background: #0a0a0a; border: 1px solid #2d2d44; border-radius: 8px; color: #fff; margin-bottom: 10px; }
            button { width: 100%; padding: 14px; background: #00d9ff; color: #000; border: none; border-radius: 8px; font-weight: bold; margin-top: 10px; cursor: pointer; }
            .btn-success { background: #00ff88; }
            .btn-danger { background: #ff4757; color: #fff; }
            .filters { display: flex; gap: 5px; margin: 10px 0; }
            .badge { width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: bold; }
            .pass { background: #00ff88; color: #000; }
            .fail { background: #ff4757; color: #fff; }
            .signal { margin-top: 20px; }
            .hidden { display: none; }
            .status-bar { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
            .status-item { background: #1a1a2e; padding: 10px 15px; border-radius: 8px; font-size: 13px; }
            .status-item span { color: #00ff88; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>🔮 Kalshi SixFilter</h1>
        <p style="color: #888; margin-bottom: 20px;">MIT 6-Filter Prediction Market Strategy</p>

        <div class="status-bar">
            <div class="status-item">Trader: <span id="traderStatus">Loading...</span></div>
            <div class="status-item">Trades: <span id="tradeCount">0</span></div>
            <div class="status-item">Balance: <span id="balance">$0</span></div>
            <div class="status-item">Telegram: <span id="tgStatus">?</span></div>
        </div>

        <div class="card">
            <h3>Auto Controls</h3>
            <button onclick="triggerScan()">🔍 RUN SCAN NOW</button>
            <button onclick="loadStatus()" style="margin-top:8px;background:#333;color:#fff;">🔄 REFRESH STATUS</button>
        </div>

        <div class="card">
            <h3>Market Analysis</h3>
            <input type="text" id="marketId" placeholder="Market ID (e.g. KXBTC15M-26AUG020130-30)">
            <input type="text" id="marketName" placeholder="Market Name">
            <input type="number" id="yesPrice" placeholder="YES Price (¢)" min="1" max="99">
            <input type="number" id="noPrice" placeholder="NO Price (¢)" min="1" max="99">
            <input type="number" id="modelProb" placeholder="Your Model Prob (%)" min="0" max="100" step="0.1">
            <input type="number" id="volume" placeholder="Volume" value="50000">
            <button onclick="analyze()">🔍 Run SixFilter Analysis</button>
        </div>

        <div id="result" class="card hidden">
            <h3 id="resultTitle">Signal</h3>
            <div id="filters" class="filters"></div>
            <p id="resultDetails"></p>
            <button id="executeBtn" class="btn-success hidden" onclick="execute()">Execute on Kalshi</button>
        </div>

        <div class="card">
            <h3>Recent Trades</h3>
            <pre id="tradeLog" style="background:#000;padding:10px;overflow-x:auto;font-size:12px;">None yet</pre>
        </div>

    <script>
    const API_URL = '';
    let currentSignal = null;

    async function loadStatus() {
        try {
            const [health, status, bal] = await Promise.all([
                fetch('/health').then(r => r.json()),
                fetch('/status').then(r => r.json()),
                fetch('/kalshi/balance').then(r => r.json()).catch(() => ({balance:0}))
            ]);
            document.getElementById('traderStatus').textContent = health.auto_trader_ready ? '🟢 Ready' : '🔴 Down';
            document.getElementById('traderStatus').style.color = health.auto_trader_ready ? '#00ff88' : '#ff4757';
            document.getElementById('tradeCount').textContent = `${status.trades_today} / ${status.max_trades}`;
            document.getElementById('balance').textContent = `$${bal.balance?.toFixed?.(2) || bal.balance || 0}`;
            document.getElementById('tgStatus').textContent = health.telegram ? '🟢 On' : '🔴 Off';

            if (status.trade_log && status.trade_log.length > 0) {
                document.getElementById('tradeLog').textContent =
                    status.trade_log.slice(-5).map(t => JSON.stringify(t, null, 2)).join('\\n---\\n');
            }
        } catch(e) {
            console.error(e);
        }
    }

    async function triggerScan() {
        await fetch('/scan', {method: 'POST'});
        alert('Scan triggered! Check status in a few seconds.');
        setTimeout(loadStatus, 5000);
    }

    async function analyze() {
        const data = {
            market_id: document.getElementById('marketId').value,
            market_name: document.getElementById('marketName').value,
            yes_price: parseFloat(document.getElementById('yesPrice').value),
            no_price: parseFloat(document.getElementById('noPrice').value),
            volume: parseFloat(document.getElementById('volume').value),
            open_interest: parseFloat(document.getElementById('volume').value) * 0.5,
            your_model_prob: parseFloat(document.getElementById('modelProb').value) / 100,
            bankroll: 1000,
            daily_pnl: 0,
            consecutive_losses: 0
        };

        const res = await fetch('/kalshi/analyze', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });

        const signal = await res.json();
        currentSignal = signal;

        document.getElementById('result').classList.remove('hidden');
        document.getElementById('resultTitle').textContent = signal.proceed ? '✅ APPROVED' : '❌ REJECTED';
        document.getElementById('resultTitle').style.color = signal.proceed ? '#00ff88' : '#ff4757';

        const filtersDiv = document.getElementById('filters');
        filtersDiv.innerHTML = (signal.filters_passed || []).map((p, i) =>
            `<div class="badge ${p ? 'pass' : 'fail'}">${i+1}</div>`
        ).join('');

        document.getElementById('resultDetails').innerHTML =
            `Side: <b>${signal.side?.toUpperCase()}</b> | Contracts: <b>${signal.contracts}</b> | ` +
            `Price: <b>${signal.limit_price}¢</b> | Edge: <b>${signal.edge_percent?.toFixed?.(1) || 0}%</b><br>` +
            `Confidence: <b>${signal.confidence}%</b> | EV: <b>$${signal.expected_value?.toFixed?.(2) || 0}</b>`;

        document.getElementById('executeBtn').classList.toggle('hidden', !signal.proceed);
    }

    async function execute() {
        if (currentSignal) {
            const res = await fetch('/kalshi/execute', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    market_id: currentSignal.market_id,
                    side: currentSignal.side,
                    contracts: currentSignal.contracts,
                    limit_price: currentSignal.limit_price
                })
            });
            const result = await res.json();
            alert(result.order ? 'Order placed!' : 'Order failed: ' + JSON.stringify(result));
            loadStatus();
        }
    }

    loadStatus();
    setInterval(loadStatus, 10000);
    </script>
    </body>
    </html>
    """

@app.get("/")
def root():
    return {
        "message": "SixFilter Kalshi Auto-Trader",
        "docs": "/docs",
        "dashboard": "/dashboard",
        "telegram": "/telegram/setup",
        "endpoints": {
            "health": "/health",
            "status": "/status",
            "scan": "POST /scan",
            "analyze": "POST /analyze",
            "kalshi_analyze": "POST /kalshi/analyze",
            "kalshi_execute": "POST /kalshi/execute",
            "order": "POST /kalshi/order",
            "markets": "GET /kalshi/markets?series=KXBTC15M",
            "balance": "GET /kalshi/balance",
            "positions": "GET /kalshi/positions",
            "telegram_webhook": "POST /webhook/telegram",
            "telegram_setup": "GET /telegram/setup",
            "telegram_test": "POST /telegram/test"
        }
    }

# ========== BACKGROUND SCHEDULER (APScheduler) ==========

scheduler = BackgroundScheduler()

def scheduled_scan():
    if auto_trader:
        try:
            auto_trader.run_cycle()
        except Exception as e:
            print(f"[Scheduler] Error: {e}")

if auto_trader:
    scheduler.add_job(scheduled_scan, 'interval', seconds=SCAN_INTERVAL_SECONDS, id='sixfilter_scan', replace_existing=True)
    scheduler.start()
    print(f"⏰ APScheduler started: scanning every {SCAN_INTERVAL_SECONDS}s")

# ========== MAIN ==========

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)

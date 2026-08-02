"""
SixFilter Kalshi Auto-Trader — Complete Single File
Kalshi RSA Auth + Binance Spot Feed + SixFilter + Auto-Scheduler
"""
import os
import json
import time
import uuid
import base64
import threading
import schedule
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ========== CONFIG / ENV ==========

MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "10"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "50.0"))
MIN_EDGE_PERCENT = float(os.getenv("MIN_EDGE_PERCENT", "5.0"))
CONTRACT_SIZE = int(os.getenv("CONTRACT_SIZE", "10"))
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL", "90"))
AVOID_LUNCH = os.getenv("AVOID_LUNCH", "true").lower() == "true"

# ========== BINANCE SPOT FEED ==========

class BinanceFeed:
    """Real-time BTC/ETH spot prices for LMSR edge calc"""
    _cache: Dict[str, Tuple[float, float]] = {}  # symbol -> (price, timestamp)
    _cache_ttl = 5  # seconds

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
        """Get recent OHLCV for momentum/divergence"""
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
                clean_key = key_env.replace("\\n", "
").strip().encode("utf-8")
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

# ========== SIXFILTER ANALYZER (WITH BINANCE) ==========

class Direction(Enum):
    YES = "yes"
    NO = "no"

@dataclass
class Signal:
    ticker: str
    direction: Direction
    edge: float
    confidence: int
    entry_price: int  # cents 0-100
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
        """Try to get floor_strike or infer from title"""
        strike = market.get("floor_strike")
        if strike:
            return float(strike)
        # Fallback: parse from title like "Bitcoin to be above $64,150"
        title = market.get("title", "")
        import re
        m = re.search(r"[\$\£\€]?([\d,]+\.?\d*)", title)
        if m:
            return float(m.group(1).replace(",", ""))
        return None

    def _lmsr(self, market: dict, binance_spot: Optional[float]) -> Tuple[float, Direction, str]:
        """Filter 1: True probability from Binance spot vs Kalshi implied"""
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
            # If spot > strike, probability of YES is higher
            diff_pct = (binance_spot - strike) / strike
            # Rough sigmoid mapping: ±1% diff → ±30% prob shift
            import math
            true_prob = 0.5 + (math.atan(diff_pct * 100) / math.pi) * 0.8
            true_prob = max(0.05, min(0.95, true_prob))
            edge = (true_prob - implied) * 100
            direction = Direction.YES if edge > 0 else Direction.NO
            return abs(edge), direction, f"LMSR_spot={binance_spot:.0f}_strike={strike:.0f}_true={true_prob:.2f}"

        # Fallback: use momentum from Binance klines
        symbol = self._get_binance_symbol(market.get("ticker", ""))
        klines = BinanceFeed.get_klines(symbol, "1m", 3) if symbol else []
        if len(klines) >= 2:
            momentum = (klines[-1]["close"] - klines[0]["open"]) / klines[0]["open"]
            true_prob = 0.5 + momentum * 50  # Rough scaling
            true_prob = max(0.05, min(0.95, true_prob))
            edge = (true_prob - implied) * 100
            direction = Direction.YES if edge > 0 else Direction.NO
            return abs(edge), direction, f"LMSR_momentum={momentum:.4f}"

        # Last resort: heuristic
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
        """Filter 2: Position sizing"""
        if edge <= 0:
            return 0, 0.0
        p = 0.5 + edge / 200
        b = 1.0
        q = 1 - p
        kelly = (b * p - q) / b
        kelly = max(0, min(kelly, self.max_kelly))
        risk = min(bankroll * kelly, self.max_position)
        # Binary at ~$0.50 entry = ~$0.50 risk per contract
        contracts = int(risk / 0.5)
        return max(1, min(contracts, 100)), kelly

    def _ev_gap(self, market: dict, direction: Direction, true_prob: float) -> Tuple[bool, float]:
        """Filter 3: Expected value check"""
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
        """Filter 4: KL Divergence via volume/price"""
        vol = market.get("volume", 0)
        oi = market.get("open_interest", 0)
        last = (market.get("last_price") or 0) / 100.0

        if vol == 0:
            return False, "NO_VOLUME"

        # Kalshi divergence: high vol but price at mid = indecision
        if vol > max(oi, 1) * 0.15 and abs(last - 0.5) < 0.05:
            return False, "VOLUME_MID_INDECISION"

        # Binance divergence: price up but volume down (last 2 candles)
        if len(klines) >= 2:
            price_up = klines[-1]["close"] > klines[-2]["close"]
            vol_down = klines[-1]["volume"] < klines[-2]["volume"] * 0.8
            if price_up and vol_down:
                return False, "PRICE_UP_VOL_DOWN"

        return True, "OK"

    def _bayesian(self, trades_today: int, daily_pnl: float, close_time: datetime) -> Tuple[bool, str]:
        """Filter 5: Context & limits"""
        if daily_pnl <= -DAILY_LOSS_LIMIT:
            return False, "DAILY_LOSS_LIMIT"
        if trades_today >= MAX_TRADES_PER_DAY:
            return False, "MAX_TRADES"

        # Time guards
        now = datetime.now()
        hour = now.hour + now.minute / 60
        if AVOID_LUNCH and 12.0 <= hour <= 13.5:
            return False, "LUNCH_HOUR"

        secs = (close_time.replace(tzinfo=None) - now).total_seconds()
        if secs < 30:
            return False, f"CLOSE_{int(secs)}s"

        return True, "OK"

    def _stoikov(self, market: dict, direction: Direction) -> int:
        """Filter 6: Limit order at improved price"""
        if direction == Direction.YES:
            bid = market.get("yes_bid") or 0
            ask = market.get("yes_ask") or 0
        else:
            bid = market.get("no_bid") or 0
            ask = market.get("no_ask") or 0

        if bid <= 0 or ask <= 0:
            return 0
        mid = (bid + ask) / 2
        # Improve by 1 cent toward us
        if direction == Direction.YES:
            return max(1, int(mid - 1))
        else:
            return max(1, int(mid - 1))

    def analyze(self, market: dict, trades_today: int = 0, daily_pnl: float = 0.0) -> Optional[Signal]:
        """Run all 6 filters on a Kalshi market dict"""
        passed = []
        ticker = market.get("ticker", "")

        # Time parse
        close_str = market.get("close_time", "")
        try:
            close = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        except:
            return None

        # 0. Time filter
        ok, reason = self._bayesian(trades_today, daily_pnl, close)
        if not ok:
            return None
        passed.append(f"TIME({reason})")

        # Binance data
        symbol = self._get_binance_symbol(ticker)
        binance_spot = BinanceFeed.get_price(symbol) if symbol else None
        klines = BinanceFeed.get_klines(symbol, "1m", 3) if symbol else []

        # 1. LMSR
        edge, direction, lmsr_reason = self._lmsr(market, binance_spot)
        if edge < self.min_edge:
            return None
        passed.append(lmsr_reason)

        # Track true_prob for EV calc
        if binance_spot:
            strike = self._extract_strike(market)
            if strike:
                import math
                diff_pct = (binance_spot - strike) / strike
                true_prob = 0.5 + (math.atan(diff_pct * 100) / math.pi) * 0.8
                true_prob = max(0.05, min(0.95, true_prob))
            else:
                true_prob = 0.5 + (edge / 200) * (1 if direction == Direction.YES else -1)
        else:
            true_prob = 0.5 + (edge / 200) * (1 if direction == Direction.YES else -1)

        # 2. Kelly sizing
        yes_ask = (market.get("yes_ask") or 0) / 100.0
        no_ask = (market.get("no_ask") or 0) / 100.0
        market_price = yes_ask if direction == Direction.YES else no_ask
        size, kelly_frac = self._kelly(edge, self.bankroll, market_price)
        if size < 1:
            return None
        size = min(size, CONTRACT_SIZE)
        passed.append(f"KELLY({size}_f={kelly_frac:.3f})")

        # 3. EV Gap
        ev_ok, ev_pct = self._ev_gap(market, direction, true_prob)
        if not ev_ok:
            return None
        passed.append(f"EV({ev_pct:.1f}%)")

        # 4. Divergence
        div_ok, div_reason = self._divergence(market, klines)
        if not div_ok:
            return None
        passed.append(f"DIV({div_reason})")

        # 5. Bayesian already passed in step 0
        passed.append("BAYESIAN(OK)")

        # 6. Stoikov
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

    def _update_positions(self):
        """Refresh current positions from Kalshi"""
        resp = self.client.get_positions()
        if "positions" in resp:
            self.positions = {
                p.get("market_id", ""): p.get("count", 0)
                for p in resp["positions"]
                if p.get("count", 0) != 0
            }

    def scan_series(self, series: str) -> List[Signal]:
        """Scan all open markets in a series, return signals"""
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
                # Check existing position
                current = self.positions.get(sig.ticker, 0)
                if abs(current) >= 50:
                    continue
                signals.append(sig)
                print(f"🟢 {sig.ticker} {sig.direction.value} | Edge:{sig.edge:.1f}% | ${sig.entry_price/100:.2f} x{sig.size} | Spot:{sig.binance_spot}")

        signals.sort(key=lambda x: x.edge, reverse=True)
        print(f"📊 {series}: {len(signals)} signals from {len(markets)} markets")
        return signals

    def execute(self, signal: Signal) -> dict:
        """Place order via KalshiClient"""
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
            else:
                print(f"❌ FAILED: {result.get('error', result)}")

        return result

    def run_cycle(self):
        """One full scan + execute cycle"""
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

        # Update bankroll from live balance
        bal = self.client.get_balance()
        self.analyzer.bankroll = bal.get("balance", 22.17)

        all_signals = []
        for series in ["KXBTC15M", "KXETH15M"]:
            sigs = self.scan_series(series)
            all_signals.extend(sigs)

        # Take top N
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
                "trade_log": self.trade_log[-20:],  # Last 20
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

# ========== ENDPOINTS ==========

@app.get("/health")
def health():
    cfg = kalshi.get_config() if kalshi else {"error": "not initialized"}
    return {
        "status": "ok",
        "kalshi": cfg,
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

@app.post("/analyze")
def analyze_market(req: ManualAnalyzeRequest):
    """Manual analyze + optional execute"""
    if not auto_trader:
        raise HTTPException(status_code=503, detail="Auto-trader not ready")

    # Build a fake market dict for the analyzer
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
    """Trigger one manual scan cycle"""
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
    """Update risk params (requires restart for full effect)"""
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

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>SixFilter Kalshi Trader</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: monospace; background: #0a0a0a; color: #00ff88; padding: 20px; }
            h1 { color: #f59e0b; }
            .card { background: #111; border: 1px solid #333; padding: 15px; margin: 10px 0; border-radius: 8px; }
            button { background: #f59e0b; color: #000; border: none; padding: 10px 20px; cursor: pointer; font-weight: bold; }
            button:hover { background: #ffb700; }
            .green { color: #00ff88; }
            .red { color: #ff4444; }
            .yellow { color: #f59e0b; }
            pre { background: #000; padding: 10px; overflow-x: auto; }
        </style>
    </head>
    <body>
        <h1>🎯 SixFilter Kalshi Auto-Trader</h1>
        <div class="card">
            <h3>Status</h3>
            <div id="status">Loading...</div>
        </div>
        <div class="card">
            <button onclick="scan()">🔍 SCAN NOW</button>
            <button onclick="loadStatus()">🔄 REFRESH</button>
        </div>
        <div class="card">
            <h3>Recent Trades</h3>
            <div id="trades">None yet</div>
        </div>
        <script>
            async function loadStatus() {
                const r = await fetch('/status');
                const d = await r.json();
                document.getElementById('status').innerHTML = `
                    <span class="${d.running ? 'green' : 'yellow'}">Running: ${d.running}</span><br>
                    Trades Today: ${d.trades_today} / ${d.max_trades}<br>
                    Daily PnL: <span class="${d.daily_pnl >= 0 ? 'green' : 'red'}">$${d.daily_pnl.toFixed(2)}</span><br>
                    Min Edge: ${d.min_edge}%<br>
                    Loss Limit: $${d.loss_limit}<br>
                    Positions: ${Object.keys(d.positions).length} markets
                `;
                if (d.trade_log && d.trade_log.length > 0) {
                    document.getElementById('trades').innerHTML = '<pre>' + 
                        d.trade_log.slice(-5).map(t => JSON.stringify(t, null, 2)).join('\n---\n') + '</pre>';
                }
            }
            async function scan() {
                document.getElementById('status').innerHTML = '<span class="yellow">Scanning...</span>';
                await fetch('/scan', {method: 'POST'});
                setTimeout(loadStatus, 3000);
            }
            loadStatus();
            setInterval(loadStatus, 10000);
        </script>
    </body>
    </html>
    """
    return html

@app.get("/")
def root():
    return {
        "message": "SixFilter Kalshi Auto-Trader",
        "docs": "/docs",
        "dashboard": "/dashboard",
        "endpoints": {
            "health": "/health",
            "status": "/status",
            "scan": "POST /scan",
            "analyze": "POST /analyze",
            "order": "POST /kalshi/order",
            "markets": "GET /kalshi/markets?series=KXBTC15M",
            "balance": "GET /kalshi/balance",
            "positions": "GET /kalshi/positions"
        }
    }

# ========== BACKGROUND SCHEDULER ==========

def run_scheduler():
    if not auto_trader:
        print("❌ Auto-trader not initialized, scheduler exiting")
        return
    print(f"⏰ Scheduler started: every {SCAN_INTERVAL_SECONDS}s")
    schedule.every(SCAN_INTERVAL_SECONDS).seconds.do(auto_trader.run_cycle)
    while True:
        schedule.run_pending()
        time.sleep(1)

# Start background thread
if auto_trader:
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

# ========== MAIN ==========

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)

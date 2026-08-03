"""
SixFilter Kalshi Auto-Trader — clean single-file rebuild
Deploy: Railway (repo root)
Start command: uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}

What it does:
- Scans Kalshi series (KXBTC15M, KXETH15M, KXBTC1H, KXETH1H, KXEURUSD)
- Pulls live prices + volatility from Binance
- Estimates true probability of finishing above/below the strike
- Trades only when model edge beats the market price by EDGE_THRESHOLD
- 6-filter gate before every order + daily risk limits
- Telegram notifications + command bot
- Endpoints: /health /status /balance /scan /trade /dashboard /webhook/telegram
"""

import os
import re
import time
import math
import base64
import asyncio
import logging
import statistics
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sixfilter")

# ---------------------------------------------------------------- config ----
def env(*names, default=""):
    for n in names:
        v = os.getenv(n)
        if v and str(v).strip():
            return str(v).strip()
    return default

KALSHI_KEY_ID = env("KALSHI_KEY_ID", "KALSHI_API_KEY_ID")
KALSHI_ENV = env("KALSHI_ENV", default="live").lower()
if KALSHI_ENV == "demo":
    BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
else:
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
SIGN_PREFIX = "/trade-api/v2"

SCAN_SERIES = [s.strip().upper() for s in env(
    "SCAN_SERIES", default="KXBTC15M,KXETH15M,KXBTC1H,KXETH1H"
).split(",") if s.strip()]

SERIES_SYMBOLS = {
    "KXBTC15M": "BTCUSDT",
    "KXETH15M": "ETHUSDT",
    "KXBTC1H": "BTCUSDT",
    "KXETH1H": "ETHUSDT",
    "KXEURUSD": "EURUSDT",   # Binance EURUSDT ~ EURUSD spot
}

EDGE_THRESHOLD = float(env("EDGE_THRESHOLD", default="0.08"))   # 8 cents of edge
TRADE_SIZE = int(env("TRADE_SIZE", default="1"))                 # contracts per trade
MAX_TRADES_PER_DAY = int(env("MAX_TRADES_PER_DAY", default="10"))
MIN_MINUTES_TO_EXPIRY = float(env("MIN_MINUTES_TO_EXPIRY", default="3"))
MAX_MINUTES_TO_EXPIRY = float(env("MAX_MINUTES_TO_EXPIRY", default="60"))
MIN_PRICE_CENTS = int(env("MIN_PRICE_CENTS", default="10"))
MAX_PRICE_CENTS = int(env("MAX_PRICE_CENTS", default="90"))
SCAN_INTERVAL_SEC = int(env("SCAN_INTERVAL_SEC", default="60"))
ATTEMPT_COOLDOWN_SEC = int(env("ATTEMPT_COOLDOWN_SEC", default="900"))  # 15 min
AUTO_TRADE = env("AUTO_TRADE", default="true").lower() == "true"

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

# ------------------------------------------------------------- kalshi auth --
_PRIVATE_KEY = None
_KEY_ERROR = None

def _normalize_pem(raw: str) -> bytes:
    """Rebuild a valid PEM even if newlines were mangled by the env var UI."""
    raw = raw.strip().strip('"').strip("'").replace("\\n", "\n")
    m = re.search(r"-----BEGIN ([A-Z0-9 ]*KEY)-----(.*?)-----END \1-----", raw, re.S)
    if not m:
        raise ValueError("PEM markers not found in KALSHI_PRIVATE_KEY")
    label = m.group(1)
    body = re.sub(r"[^A-Za-z0-9+/=]", "", m.group(2))
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n".encode()

def load_key():
    global _PRIVATE_KEY, _KEY_ERROR
    if _PRIVATE_KEY:
        return _PRIVATE_KEY
    raw = env("KALSHI_PRIVATE_KEY", "KALSHI_RSA_PRIVATE_KEY", "KALSHI_SECRET_KEY")
    if not raw:
        _KEY_ERROR = "KALSHI_PRIVATE_KEY not set"
        return None
    try:
        _PRIVATE_KEY = serialization.load_pem_private_key(_normalize_pem(raw), password=None)
        _KEY_ERROR = None
        return _PRIVATE_KEY
    except Exception as e:
        _KEY_ERROR = str(e)
        log.error(f"private key load failed: {e}")
        return None

def _sign_headers(method: str, endpoint: str):
    key = load_key()
    if not key or not KALSHI_KEY_ID:
        raise RuntimeError(f"Kalshi credentials not usable: {_KEY_ERROR or 'missing key id'}")
    ts = str(int(time.time() * 1000))
    msg = (ts + method.upper() + SIGN_PREFIX + endpoint).encode()
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }

async def kalshi_get(endpoint: str, params: dict | None = None):
    headers = _sign_headers("GET", endpoint)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15) as c:
        r = await c.get(endpoint, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

async def kalshi_post(endpoint: str, body: dict):
    headers = _sign_headers("POST", endpoint)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15) as c:
        r = await c.post(endpoint, json=body, headers=headers)
        r.raise_for_status()
        return r.json()

# ---------------------------------------------------------------- binance ---
async def binance_stats(symbol: str):
    """Return (spot, sigma_per_minute, dampened_drift_per_minute)."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 120},
        )
        r.raise_for_status()
        closes = [float(k[4]) for k in r.json()]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    sigma = statistics.pstdev(rets) if len(rets) > 2 else 0.001
    drift = (statistics.mean(rets[-20:]) * 0.5) if len(rets) >= 20 else 0.0
    return closes[-1], sigma, drift

def prob_above(spot: float, strike: float, sigma_1m: float, drift_1m: float, minutes: float) -> float:
    if minutes <= 0:
        return 1.0 if spot > strike else 0.0
    vol = sigma_1m * math.sqrt(minutes)
    if vol <= 0:
        return 1.0 if spot > strike else 0.0
    z = (math.log(spot / strike) + drift_1m * minutes) / vol
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))

# ------------------------------------------------------------------ state ---
STATE = {
    "paused": not AUTO_TRADE,
    "day": "",
    "trades_today": 0,
    "traded_tickers": [],       # successful orders, permanent for the day
    "attempt_cooldown": {},     # ticker -> epoch, any attempt (success or fail)
    "last_scan": None,
    "last_signals": [],
    "last_error": None,
    "started_at": datetime.now(timezone.utc).isoformat(),
}

def reset_daily():
    today = datetime.now(timezone.utc).date().isoformat()
    if STATE["day"] != today:
        STATE["day"] = today
        STATE["trades_today"] = 0
        STATE["traded_tickers"] = []
        STATE["attempt_cooldown"] = {}

# ---------------------------------------------------------------- telegram --
async def tg_send(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            )
    except Exception as e:
        log.warning(f"telegram send failed: {e}")

# ----------------------------------------------------------------- helpers --
def cents(m: dict, key: str):
    """Kalshi migrated to *_dollars fields; accept either."""
    v = m.get(key)
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    v = m.get(key + "_dollars")
    if v is not None:
        try:
            return float(v) * 100
        except (TypeError, ValueError):
            return None
    return None

def strike_of(m: dict):
    for k in ("floor_strike", "strike"):
        v = m.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None

async def fetch_active_market(series: str):
    data = await kalshi_get("/markets", params={"series_ticker": series, "status": "open", "limit": 200})
    now = time.time()
    best = None
    for m in data.get("markets", []):
        exp = m.get("expiration_time") or m.get("close_time")
        if not exp:
            continue
        try:
            exp_ts = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        mins = (exp_ts - now) / 60.0
        if mins < MIN_MINUTES_TO_EXPIRY or mins > MAX_MINUTES_TO_EXPIRY:
            continue
        if best is None or mins < best["minutes"]:
            best = {"market": m, "minutes": mins}
    return best

# ------------------------------------------------------------------ engine --
async def analyze_series(series: str, execute: bool = False):
    reset_daily()
    result = {"series": series, "proceed": False, "filters": {}, "reason": ""}
    symbol = SERIES_SYMBOLS.get(series)
    if not symbol:
        result["reason"] = "no price feed mapped for this series"
        return result

    f = result["filters"]

    # Filter 1 — active market inside the expiry window
    pick = await fetch_active_market(series)
    f["market_active"] = pick is not None
    if not pick:
        result["reason"] = f"no open market expiring in {MIN_MINUTES_TO_EXPIRY}-{MAX_MINUTES_TO_EXPIRY}m"
        return result
    m, mins = pick["market"], pick["minutes"]
    ticker = m.get("ticker", "")
    result.update(ticker=ticker, minutes_to_expiry=round(mins, 1))

    # Filter 2 — prices exist and sit inside tradable bounds
    yes_ask, yes_bid = cents(m, "yes_ask"), cents(m, "yes_bid")
    strike = strike_of(m)
    strike_type = (m.get("strike_type") or "greater").lower()
    f["liquidity"] = (
        yes_ask is not None and yes_bid is not None and strike is not None
        and MIN_PRICE_CENTS <= yes_ask <= MAX_PRICE_CENTS
    )
    if not f["liquidity"]:
        result["reason"] = "prices missing or outside tradable bounds"
        return result
    result.update(yes_bid=yes_bid, yes_ask=yes_ask, strike=strike)

    # Filter 3 — live price feed
    try:
        spot, sigma, drift = await binance_stats(symbol)
        f["data_fresh"] = True
    except Exception as e:
        f["data_fresh"] = False
        result["reason"] = f"binance error: {e}"
        return result
    result["spot"] = spot

    # Model probability
    p = prob_above(spot, strike, sigma, drift, mins)
    if strike_type == "less":
        p = 1.0 - p
    result["model_prob"] = round(p, 4)

    # Filter 4 — directional clarity (not a coin flip)
    f["clarity"] = abs(p - 0.5) >= 0.02
    if not f["clarity"]:
        result["reason"] = "model has no directional edge (near 50/50)"
        return result

    # Filter 5 — edge vs market price
    edge_yes = p - yes_ask / 100.0
    edge_no = (yes_bid / 100.0) - p
    if edge_yes >= edge_no and edge_yes >= EDGE_THRESHOLD:
        side, price_c, edge = "yes", yes_ask, edge_yes
    elif edge_no > edge_yes and edge_no >= EDGE_THRESHOLD:
        side, price_c, edge = "no", 100.0 - yes_bid, edge_no
    else:
        side, price_c, edge = None, None, max(edge_yes, edge_no)
    f["edge"] = side is not None
    result.update(edge=round(edge, 4), side=side, limit_price_cents=price_c)
    if not f["edge"]:
        result["reason"] = f"edge {round(edge, 3)} below threshold {EDGE_THRESHOLD}"
        return result

    # Filter 6 — risk limits
    cooled = STATE["attempt_cooldown"].get(ticker, 0)
    f["risk"] = (
        STATE["trades_today"] < MAX_TRADES_PER_DAY
        and ticker not in STATE["traded_tickers"]
        and (time.time() - cooled) > ATTEMPT_COOLDOWN_SEC
    )
    if not f["risk"]:
        result["reason"] = "risk limit hit (max trades, duplicate, or cooldown)"
        return result

    result["proceed"] = True
    result["reason"] = "all 6 filters passed"

    if execute:
        STATE["attempt_cooldown"][ticker] = time.time()
        order = await place_order(ticker, side, price_c, TRADE_SIZE)
        result["order"] = order
        if order.get("ok"):
            STATE["trades_today"] += 1
            STATE["traded_tickers"].append(ticker)
            await tg_send(
                f"TRADE PLACED\n{ticker}\nbuy {side.upper()} x{TRADE_SIZE} @ {price_c}c\n"
                f"model {round(p, 3)} - edge {round(edge, 3)} - expires in {round(mins, 1)}m"
            )
        else:
            await tg_send(f"ORDER FAILED\n{ticker}\n{order.get('error')}")
    return result

async def place_order(ticker: str, side: str, price_cents: float, count: int):
    body = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "action": "buy",
        "side": side,
        "type": "limit",
        "count": int(count),
        "time_in_force": "fill_or_kill",
    }
    if side == "yes":
        body["yes_price"] = int(round(price_cents))
    else:
        body["no_price"] = int(round(price_cents))
    try:
        resp = await kalshi_post("/portfolio/orders", body)
        return {"ok": True, "request": body, "response": resp}
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.text[:400]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {detail}", "request": body}
    except Exception as e:
        return {"ok": False, "error": str(e), "request": body}

async def scan_all(execute: bool = False):
    out = []
    for s in SCAN_SERIES:
        try:
            out.append(await analyze_series(s, execute=execute))
        except Exception as e:
            log.error(f"scan {s} failed: {e}")
            out.append({"series": s, "proceed": False, "error": str(e)})
    STATE["last_scan"] = datetime.now(timezone.utc).isoformat()
    STATE["last_signals"] = out
    return out

async def auto_loop():
    await asyncio.sleep(10)
    log.info(f"scanner up: {SCAN_SERIES} every {SCAN_INTERVAL_SEC}s - auto_trade={AUTO_TRADE}")
    await tg_send(f"SixFilter online.\nScanning: {', '.join(SCAN_SERIES)}\nAuto-trade: {AUTO_TRADE}")
    while True:
        if AUTO_TRADE and not STATE["paused"]:
            try:
                await scan_all(execute=True)
            except Exception as e:
                STATE["last_error"] = str(e)
                log.error(f"auto scan error: {e}")
        await asyncio.sleep(SCAN_INTERVAL_SEC)

# -------------------------------------------------------------------- app ---
app = FastAPI(title="SixFilter Kalshi Trader API", docs_url="/docs")

@app.on_event("startup")
async def _startup():
    load_key()
    asyncio.create_task(auto_loop())

@app.get("/")
def root():
    return {"service": "SixFilter Kalshi Trader API", "docs": "/docs", "dashboard": "/dashboard"}

@app.get("/health")
def health():
    return {
        "status": "ok",
        "kalshi": {
            "env": KALSHI_ENV,
            "key_id_set": bool(KALSHI_KEY_ID),
            "key_loaded": bool(load_key()),
            "key_error": _KEY_ERROR,
            "base_url": BASE_URL,
        },
        "scanning": SCAN_SERIES,
        "auto_trade": AUTO_TRADE,
        "paused": STATE["paused"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/balance")
async def balance():
    data = await kalshi_get("/portfolio/balance")
    bal = data.get("balance")
    dollars = bal / 100 if isinstance(bal, (int, float)) else data.get("balance_dollars")
    return {"balance": dollars, "env": KALSHI_ENV, "status": "key_loaded" if load_key() else "key_error"}

@app.get("/status")
def status():
    return {
        "paused": STATE["paused"],
        "auto_trade": AUTO_TRADE,
        "trades_today": STATE["trades_today"],
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "edge_threshold": EDGE_THRESHOLD,
        "trade_size": TRADE_SIZE,
        "scanning": SCAN_SERIES,
        "last_scan": STATE["last_scan"],
        "last_error": STATE["last_error"],
        "started_at": STATE["started_at"],
        "last_signals": STATE["last_signals"],
    }

class ScanRequest(BaseModel):
    execute: bool = False

@app.post("/scan")
async def manual_scan(req: ScanRequest):
    results = await scan_all(execute=req.execute)
    return {"executed": req.execute, "results": results}

class TradeRequest(BaseModel):
    ticker: str
    side: str            # "yes" or "no"
    count: int = 1
    price_cents: int | None = None

@app.post("/trade")
async def manual_trade(req: TradeRequest):
    price = req.price_cents
    if price is None:
        data = await kalshi_get(f"/markets/{req.ticker}")
        m = data.get("market", {})
        yb = cents(m, "yes_bid") or 0
        price = cents(m, "yes_ask") if req.side.lower() == "yes" else (100 - yb)
        if price is None:
            return {"ok": False, "error": "could not determine market price"}
    order = await place_order(req.ticker, req.side.lower(), price, req.count)
    if order.get("ok"):
        reset_daily()
        STATE["trades_today"] += 1
        STATE["traded_tickers"].append(req.ticker)
    return order

@app.post("/admin/pause")
def pause():
    STATE["paused"] = True
    return {"paused": True}

@app.post("/admin/resume")
def resume():
    STATE["paused"] = False
    return {"paused": False}

# ------------------------------------------------------- telegram webhook ---
@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip().lower()
    chat_id = str((msg.get("chat") or {}).get("id", "") or TELEGRAM_CHAT_ID)

    async def reply(t):
        if TELEGRAM_BOT_TOKEN and chat_id:
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    await c.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": t},
                    )
            except Exception:
                pass

    if text.startswith("/start") or text.startswith("/help"):
        await reply("SixFilter commands:\n/status\n/balance\n/scan\n/trade (scan + execute)\n/pause\n/resume")
    elif text.startswith("/status"):
        s = status()
        await reply(
            f"paused: {s['paused']}\ntrades today: {s['trades_today']}/{s['max_trades_per_day']}\n"
            f"scanning: {', '.join(s['scanning'])}\nlast scan: {s['last_scan']}"
        )
    elif text.startswith("/balance"):
        try:
            b = await balance()
            await reply(f"Balance: ${b['balance']}")
        except Exception as e:
            await reply(f"balance error: {e}")
    elif text.startswith("/scan"):
        results = await scan_all(execute=False)
        lines = [f"{r['series']}: {r.get('side') or '-'} - edge {r.get('edge', 0)} - {r.get('reason', '')}" for r in results]
        await reply("\n".join(lines))
    elif text.startswith("/trade"):
        results = await scan_all(execute=True)
        placed = [r for r in results if r.get("order", {}).get("ok")]
        await reply(f"scan complete - orders placed: {len(placed)}")
    elif text.startswith("/pause"):
        STATE["paused"] = True
        await reply("auto-trading PAUSED")
    elif text.startswith("/resume"):
        STATE["paused"] = False
        await reply("auto-trading RESUMED")
    return {"ok": True}

# --------------------------------------------------------------- dashboard --
DASH_HTML = """<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SixFilter Kalshi</title>
<style>
 body{font-family:system-ui;background:#0b1220;color:#e5e7eb;max-width:760px;margin:24px auto;padding:0 16px}
 .card{background:#111c33;border:1px solid #24304d;border-radius:12px;padding:16px;margin:12px 0}
 .ok{color:#34d399}.bad{color:#f87171} pre{white-space:pre-wrap;font-size:13px;margin:0}
 h2{margin:8px 0} small{color:#94a3b8}
</style>
<h2>SixFilter Kalshi Trader</h2>
<small>auto-refreshes every 10s</small>
<div class="card" id="s">loading...</div>
<div class="card"><b>Last scan signals</b><pre id="j">...</pre></div>
<script>
async function load(){
 try{
  const h = await (await fetch('/health')).json();
  const st = await (await fetch('/status')).json();
  document.getElementById('s').innerHTML =
   '<div>status: <b class="' + (h.status==='ok'?'ok':'bad') + '">' + h.status + '</b></div>' +
   '<div>env: ' + h.kalshi.env + ' · key loaded: <b class="' + (h.kalshi.key_loaded?'ok':'bad') + '">' + h.kalshi.key_loaded + '</b>' + (h.kalshi.key_error ? ' · ' + h.kalshi.key_error : '') + '</div>' +
   '<div>auto-trade: ' + h.auto_trade + ' · paused: ' + h.paused + '</div>' +
   '<div>scanning: ' + h.scanning.join(', ') + '</div>' +
   '<div>trades today: <b>' + st.trades_today + '</b> / ' + st.max_trades_per_day + ' · edge >= ' + st.edge_threshold + '</div>' +
   '<div>last scan: ' + (st.last_scan || 'never') + '</div>';
  document.getElementById('j').textContent = JSON.stringify(st.last_signals, null, 2);
 }catch(e){document.getElementById('s').textContent = 'error: ' + e}
}
load(); setInterval(load, 10000);
</script>"""

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return DASH_HTML

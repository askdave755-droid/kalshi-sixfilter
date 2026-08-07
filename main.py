"""
SixFilter Kalshi Auto-Trader — patched build (Aug 7, 2026)
Deploy: Railway (repo root)
Start command: uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}

PATCHES vs previous build:
1. MIN_PRICE_CENTS default 48 -> 55 (ledger proved <55c entries lose money:
   30-45c bucket went 10% win rate, -$6.33; 55-75c runs 77-86% win rate)
2. EV floor: edge must clear EDGE_THRESHOLD + estimated taker fee.
   Kalshi taker fee ~= 0.07 * C * P * (1-P). A raw 8c edge at 58c is really
   ~6.3c after fees. Filter 5 now prices that in.
3. Fill-floor guard hardened: 1.5s settle wait + one retry + loud logging
   when the fill price cannot be found (silent None was letting falling
   knives through to settlement).
4. New /backtest/pnl endpoint: replays strategy P&L (not just calibration)
   over historical windows using the production model + price-band logic.
5. Filter 2 two-sided liquidity check: price floor now applies to the side
   being traded (YES ask OR NO price = 100 - yes_bid). Previously down-leaning
   markets were rejected before NO could be evaluated -> 85 YES / 1 NO skew.
6. Fill-price lookup auth fix: /portfolio/fills query params now go through
   kalshi_get's params arg. Embedding them in the URL broke the Kalshi request
   signature (401 Unauthorized on every lookup) which disabled the crash-fill
   dump guard. Confirmed in Railway logs Aug 7.
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
    "KXEURUSD": "EURUSDT",  # Binance EURUSDT ~ EURUSD spot
}

def env_int(*names, default=0):
    """Parse ints robustly: '1', '1.0', '2.0' all work."""
    raw = env(*names, default=str(default))
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return default

def env_float(*names, default=0.0):
    raw = env(*names, default=str(default))
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default

def _edge_value():
    v = env_float("EDGE_THRESHOLD", "MIN_EDGE_PERCENT", default=0.08)
    if v > 1:  # MIN_EDGE_PERCENT may be given as a percent: 8 -> 0.08
        v = v / 100.0
    return v

EDGE_THRESHOLD = _edge_value()          # minimum model-vs-market edge
TRADE_SIZE = env_int("TRADE_SIZE", "CONTRACT_SIZE", default=1)
MAX_TRADES_PER_DAY = env_int("MAX_TRADES_PER_DAY", default=10)
DAILY_LOSS_LIMIT = env_float("DAILY_LOSS_LIMIT", default=0.0)  # dollars/day cap; 0 = off
MIN_MINUTES_TO_EXPIRY = env_float("MIN_MINUTES_TO_EXPIRY", default=3.0)
MAX_MINUTES_TO_EXPIRY = env_float("MAX_MINUTES_TO_EXPIRY", default=60.0)
# PATCH 1: 48 -> 55. The Aug 2-5 ledger: <55c entries = 10-17% win rate,
# -$6.69 combined. 55-75c entries = 77-86% win rate, +$5.29.
MIN_PRICE_CENTS = env_int("MIN_PRICE_CENTS", default=55)
MAX_PRICE_CENTS = env_int("MAX_PRICE_CENTS", default=90)
SCAN_INTERVAL_SEC = env_int("SCAN_INTERVAL_SEC", "SCAN_INTERVAL", default=60)
ATTEMPT_COOLDOWN_SEC = env_int("ATTEMPT_COOLDOWN_SEC", default=900)  # 15 min
# Extra cents bid past the quoted price so IOC orders still fill when the
# botted books flicker between scan and order arrival. 0 = exact price only.
TAKER_BUFFER_CENTS = env_float("TAKER_BUFFER_CENTS", default=1.0)
# Crash-fill guard: if a fill still lands below this price, sell it straight
# back. PATCH 1: raised to match the new MIN_PRICE floor.
FILL_FLOOR_CENTS = env_float("FILL_FLOOR_CENTS", default=55.0)
# Take-profit watchdog: once a position's market bid is this many cents above
# entry, sell it back before settlement and lock the gain. 0 = disabled.
TAKE_PROFIT_CENTS = env_float("TAKE_PROFIT_CENTS", default=0.0)
AUTO_TRADE = env("AUTO_TRADE", default="true").lower() == "true"

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

# PATCH 2: Kalshi taker fee model: fee_dollars = ceil(0.07 * C * P * (1-P) * 100)/100
# per contract where P is price in dollars. We use it as an EV floor so the
# quoted edge must clear threshold + fee, not just threshold.
def kalshi_taker_fee_cents(price_cents: float, contracts: float = 1.0) -> float:
    p = max(0.01, min(0.99, price_cents / 100.0))
    fee = 0.07 * contracts * p * (1.0 - p)
    return math.ceil(fee * 100.0)  # cents, rounded up like Kalshi does

# ---------------------------------------------------- polymarket scanner ----
# Read-only intelligence feed. No Polymarket account or keys needed.
POLY_ENABLED = env("POLY_ENABLED", default="true").lower() in ("1", "true", "yes")
POLY_SCAN_SEC = env_int("POLY_SCAN_SEC", default=300)
POLY_WHALE_MIN_USD = env_float("POLY_WHALE_MIN_USD", default=25000.0)
POLY_GAP_ALERT_C = env_float("POLY_GAP_ALERT_C", default=3.0)
POLY_WATCH_KEYWORDS = [w.strip().lower() for w in env(
    "POLY_WATCH_KEYWORDS", default="bitcoin,ethereum").split(",") if w.strip()]
POLY_KALSHI_PAIRS = []
for _pair in env("POLY_KALSHI_PAIRS", default="").split(","):
    if ":" in _pair:
        _sub, _tick = _pair.split(":", 1)
        POLY_KALSHI_PAIRS.append((_sub.strip().lower(), _tick.strip()))
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_DATA = "https://data-api.polymarket.com"

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
# Recalibration constants, sized from the 7-day backtest (~3,300 windows per
# horizon per asset): >10min out the model's extremes ran 10-14pts hot, and
# ETH mean-reverts ~10pts after big 15m moves while BTC barely does (~2.5pts).
EARLY_SHRINK_MINUTES = 10.0
EARLY_SHRINK_FACTOR = 0.88
REVERSION = {"ETHUSDT": 0.10, "BTCUSDT": 0.025}

def recalibrate(p: float, mins_left: float, r15: float, sigma_1m: float, symbol: str) -> float:
    """Data-driven corrections to the raw model probability."""
    if mins_left > EARLY_SHRINK_MINUTES:
        p = 0.5 + (p - 0.5) * EARLY_SHRINK_FACTOR
    big = 1.5 * sigma_1m * math.sqrt(15)
    if abs(r15) > big and mins_left > 4:
        rev = REVERSION.get(symbol, 0.05) * min((mins_left - 4) / 9.0, 1.0)
        p += rev if r15 < 0 else -rev  # big down-move -> up-reversion, and vice versa
    return min(max(p, 0.01), 0.99)

async def binance_stats(symbol: str):
    """Return (spot, sigma_per_minute, dampened_drift_per_minute, r15).
    r15 = log return over the trailing 15 minutes, used by recalibrate()."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 120},
        )
        r.raise_for_status()
        closes = [float(k[4]) for k in r.json()]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    sigma = statistics.pstdev(rets) if len(rets) > 2 else 0.001
    # Momentum barely persists at 15-min scale; keep only a whisper of tilt.
    drift = (statistics.mean(rets[-20:]) * 0.15) if len(rets) >= 20 else 0.0
    r15 = math.log(closes[-1] / closes[-15]) if len(closes) >= 16 else 0.0
    return closes[-1], sigma, drift, r15

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
    "spent_today_cents": 0.0,
    "traded_tickers": [],
    "open_positions": [],
    "attempt_cooldown": {},
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
        STATE["spent_today_cents"] = 0.0
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
    """Pick the tradable market closest to its trading close.
    Returns (best_pick_or_None, minutes_to_nearest_close_or_None)."""
    data = await kalshi_get("/markets", params={"series_ticker": series, "limit": 200})
    now = time.time()
    best = None
    nearest_min = None
    for m in data.get("markets", []):
        status = (m.get("status") or "").lower()
        if status not in ("open", "initialized", "active"):
            continue
        exp = m.get("close_time") or m.get("expiration_time")
        if not exp:
            continue
        try:
            exp_ts = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        mins = (exp_ts - now) / 60.0
        if mins > 0 and (nearest_min is None or mins < nearest_min):
            nearest_min = mins
        if mins < MIN_MINUTES_TO_EXPIRY or mins > MAX_MINUTES_TO_EXPIRY:
            continue
        if best is None or mins < best["minutes"]:
            best = {"market": m, "minutes": mins}
    return best, nearest_min

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
    pick, nearest_min = await fetch_active_market(series)
    f["market_active"] = pick is not None
    if not pick:
        extra = f" (nearest closes in {round(nearest_min)}m)" if nearest_min else ""
        result["reason"] = f"no tradable market closing in {MIN_MINUTES_TO_EXPIRY}-{MAX_MINUTES_TO_EXPIRY}m{extra}"
        return result
    m, mins = pick["market"], pick["minutes"]
    ticker = m.get("ticker", "")
    result.update(ticker=ticker, minutes_to_expiry=round(mins, 1))

    # Filter 2 — prices exist and sit inside tradable bounds.
    # PATCH 5: check BOTH sides. A down-leaning market has a cheap YES ask
    # but an expensive NO (100 - yes_bid). Checking yes_ask alone made NO
    # trades structurally impossible (Aug 2-5 ledger: 85 YES / 1 NO).
    yes_ask, yes_bid = cents(m, "yes_ask"), cents(m, "yes_bid")
    strike = strike_of(m)
    strike_type = (m.get("strike_type") or "greater").lower()
    result.update(
        market_status=(m.get("status") or "").lower(),
        yes_bid=yes_bid, yes_ask=yes_ask, strike=strike,
    )
    no_price = (100.0 - yes_bid) if yes_bid is not None else None
    yes_ok = yes_ask is not None and MIN_PRICE_CENTS <= yes_ask <= MAX_PRICE_CENTS
    no_ok = no_price is not None and MIN_PRICE_CENTS <= no_price <= MAX_PRICE_CENTS
    f["liquidity"] = (
        yes_ask is not None and yes_bid is not None and strike is not None
        and (yes_ok or no_ok)
    )
    if not f["liquidity"]:
        result["reason"] = (
            f"prices missing or outside tradable bounds "
            f"(status={result['market_status']}, bid={yes_bid}, ask={yes_ask}, strike={strike})"
        )
        return result

    # Filter 3 — live price feed
    try:
        spot, sigma, drift, r15 = await binance_stats(symbol)
        f["data_fresh"] = True
    except Exception as e:
        f["data_fresh"] = False
        result["reason"] = f"binance error: {e}"
        return result
    result["spot"] = spot

    # Model probability. Settlement uses a 60-second BRTI average, so evaluate
    # at T-0.5min (rough midpoint of the settlement window).
    t_eff = max(mins - 0.5, 0.25)
    p = prob_above(spot, strike, sigma, drift, t_eff)
    p = recalibrate(p, mins, r15, sigma, symbol)
    if strike_type == "less":
        p = 1.0 - p
    result["model_prob"] = round(p, 4)

    # Filter 4 — directional clarity (not a coin flip)
    f["clarity"] = abs(p - 0.5) >= 0.02
    if not f["clarity"]:
        result["reason"] = "model has no directional edge (near 50/50)"
        return result

    # Filter 5 — edge vs market, WITH the Consensus Gate AND the EV floor.
    # Consensus Gate: never fight the market's directional lean
    # (disagreement trades went ~0-13, agreement trades 6-1).
    # PATCH 2 (EV floor): edge must clear EDGE_THRESHOLD + taker fee.
    # A "8c edge" at a 58c entry is really ~6.3c after Kalshi's fee formula.
    mid = (yes_bid + yes_ask) / 2.0
    edge_yes = p - yes_ask / 100.0
    edge_no = (yes_bid / 100.0) - p

    fee_yes_c = kalshi_taker_fee_cents(yes_ask, TRADE_SIZE) / max(TRADE_SIZE, 1)
    fee_no_c = kalshi_taker_fee_cents(100.0 - yes_bid, TRADE_SIZE) / max(TRADE_SIZE, 1)
    floor_yes = EDGE_THRESHOLD + fee_yes_c / 100.0
    floor_no = EDGE_THRESHOLD + fee_no_c / 100.0

    if edge_yes >= edge_no and edge_yes >= floor_yes and mid >= 50:
        side, price_c, edge = "yes", yes_ask, edge_yes
    elif edge_no > edge_yes and edge_no >= floor_no and mid < 50:
        side, price_c, edge = "no", 100.0 - yes_bid, edge_no
    else:
        side, price_c, edge = None, None, max(edge_yes, edge_no)
    f["edge"] = side is not None
    result.update(edge=round(edge, 4), side=side, limit_price_cents=price_c,
                  ev_floor=round(min(floor_yes, floor_no), 4))
    if not f["edge"]:
        leaning = "up" if mid >= 50 else "down"
        would = "yes" if edge_yes >= edge_no else "no"
        raw_floor = floor_yes if would == "yes" else floor_no
        if (would == "yes") != (mid >= 50) and max(edge_yes, edge_no) >= raw_floor:
            result["reason"] = f"edge {round(edge, 3)} but AGAINST market lean ({leaning}) - consensus gate"
        elif max(edge_yes, edge_no) >= EDGE_THRESHOLD:
            result["reason"] = f"edge {round(edge, 3)} dies to fees (EV floor {round(raw_floor, 3)})"
        else:
            result["reason"] = f"edge {round(edge, 3)} below threshold {EDGE_THRESHOLD}"
        return result

    # Filter 6 — risk limits (trade count, duplicates, cooldown, daily spend cap)
    cooled = STATE["attempt_cooldown"].get(ticker, 0)
    cost_cents = (price_c or 0) * TRADE_SIZE
    spend_cap_cents = DAILY_LOSS_LIMIT * 100.0
    over_spend_cap = spend_cap_cents > 0 and (STATE["spent_today_cents"] + cost_cents) > spend_cap_cents
    f["risk"] = (
        STATE["trades_today"] < MAX_TRADES_PER_DAY
        and ticker not in STATE["traded_tickers"]
        and (time.time() - cooled) > ATTEMPT_COOLDOWN_SEC
        and not over_spend_cap
    )
    if not f["risk"]:
        why = "daily spend cap" if over_spend_cap else "max trades, duplicate, or cooldown"
        result["reason"] = f"risk limit hit ({why})"
        return result

    result["proceed"] = True
    result["reason"] = "all 6 filters passed"

    if execute:
        STATE["attempt_cooldown"][ticker] = time.time()
        # GUARD 1 - fresh-quote re-check at order time.
        ok_fresh, fresh_px, fresh_mid = await fresh_gate_recheck(ticker, side)
        if not ok_fresh:
            result["proceed"] = False
            result["reason"] = "fresh-quote abort: book moved against the gate"
            await tg_send(
                f"ORDER ABORTED\n{ticker}\nbook moved against the gate at order time"
                f" (mid now {'?' if fresh_mid is None else round(fresh_mid, 1)}c) - no order sent")
            return result
        price_c = fresh_px  # re-price off the LIVE book, not the stale snapshot
        order = await place_order(ticker, side, price_c, TRADE_SIZE)
        result["order"] = order
        if order.get("ok"):
            filled_n = float(order.get("filled") or 0)
            if filled_n > 0:
                # Only real fills consume the daily trade/spend caps.
                STATE["trades_today"] += 1
                STATE["spent_today_cents"] += (price_c + TAKER_BUFFER_CENTS) * min(filled_n, float(TRADE_SIZE))
                STATE["traded_tickers"].append(ticker)
                pos = {"ticker": ticker, "side": side,
                       "count": min(filled_n, float(TRADE_SIZE)),
                       "entry_c": price_c + TAKER_BUFFER_CENTS, "ts": time.time()}
                STATE["open_positions"].append(pos)
                tag = "FILLED" if filled_n >= TRADE_SIZE else f"PARTIAL {filled_n:g}/{TRADE_SIZE}"
            else:
                tag = "NOT FILLED (book moved - no cost, not counted)"
            await tg_send(
                f"ORDER {tag}\n{ticker}\nbuy {side.upper()} x{TRADE_SIZE} @ {round(price_c, 1)}c\n"
                f"model {round(p, 3)} - edge {round(edge, 3)} - expires in {round(mins, 1)}m"
            )
            if filled_n > 0:
                # GUARD 2 - crash-fill dump (PATCH 3: hardened lookup).
                fill_px = await actual_fill_price_cents(ticker, side, order)
                if fill_px is not None and fill_px < FILL_FLOOR_CENTS:
                    if await close_position(
                        pos, f"crash-fill guard: filled {round(fill_px, 1)}c < {FILL_FLOOR_CENTS:g}c"):
                        STATE["open_positions"].remove(pos)
                elif fill_px is None:
                    # PATCH 3: never stay silent. If we cannot verify the fill
                    # price, alert so you can eyeball it in the app.
                    await tg_send(
                        f"FILL PRICE UNVERIFIED\n{ticker}\ncould not read fill from ledger - "
                        f"check the app. If it filled below {FILL_FLOOR_CENTS:g}c, sell it now.")
        else:
            await tg_send(f"ORDER FAILED\n{ticker}\n{order.get('error')}")
    return result

# --------------------------------------------------- fill-quality guards ---
async def fresh_gate_recheck(ticker: str, side: str):
    """Re-read the live book immediately before ordering. Returns
    (ok, fresh_price_c, mid): ok=False means abort - do not send the order."""
    try:
        data = await kalshi_get(f"/markets/{ticker}")
        m = data.get("market", {})
        if str(m.get("status", "")).lower() not in ("open", "active", "initialized"):
            return False, None, None
        bid, ask = cents(m, "yes_bid"), cents(m, "yes_ask")
        if bid is None or ask is None:
            return False, None, None
        mid = (bid + ask) / 2.0
        if side == "yes":
            return (mid >= 50.0), ask, mid       # re-priced at the LIVE ask
        return (mid < 50.0), (100.0 - bid), mid  # NO priced off the LIVE bid
    except Exception as e:
        log.error(f"fresh quote {ticker}: {e}")
        return False, None, None

async def actual_fill_price_cents(ticker: str, side: str, order: dict):
    """Our real execution price in `side` cents (None if it can't be found).

    PATCH 3: 0.7s was racing the ledger - fills often post 1-2s later, the
    lookup returned None, and the crash-fill guard silently skipped. Now we
    wait 1.5s, retry once after another 2s, and log loudly on failure."""
    oid = ((order.get("response") or {}).get("order") or {}).get("order_id")
    for attempt, wait in enumerate((1.5, 2.0), start=1):
        try:
            await asyncio.sleep(wait)
            # PATCH 6: params must go through kalshi_get's params arg, NOT in
            # the URL - the auth signature covers the path only, and signing
            # a URL with a query string gets a 401 from Kalshi. This was the
            # root cause of every "FILL PRICE UNVERIFIED" alert (Aug 7 logs).
            data = await kalshi_get("/portfolio/fills", params={"ticker": ticker, "limit": 5})
            fills = data.get("fills") or []
            if not fills:
                log.warning(f"fill price {ticker}: no fills yet (attempt {attempt})")
                continue
            pick = None
            if oid:
                pick = next((f0 for f0 in fills if f0.get("order_id") == oid), None)
            if pick is None and attempt == 2:
                pick = fills[0]  # newest fill on this ticker: ours (one order per cooldown)
            if pick is None:
                continue
            raw = pick.get("yes_price") if side == "yes" else pick.get("no_price")
            if raw is None:
                raw = pick.get("price")
            if raw is None:
                # dollar-encoded variants
                raw = (pick.get("yes_price_dollars") if side == "yes"
                       else pick.get("no_price_dollars")) or pick.get("price_dollars")
                if raw is not None:
                    return float(raw) * 100.0
                continue
            v = float(raw)
            return v * 100.0 if v <= 1.0 else v  # tolerate dollar or cent encoding
        except Exception as e:
            log.error(f"fill price {ticker} attempt {attempt}: {e}")
    log.error(f"FILL PRICE NOT FOUND for {ticker} after retries - guard skipped")
    return None

async def place_order(ticker: str, side: str, price_cents: float, count: int, reduce_only: bool = False):
    """Kalshi Create Order V2: /portfolio/events/orders.
    - buy YES at p cents -> side="bid", price=p/100
    - buy NO at q cents -> side="ask", price=(100-q)/100
    """
    if side == "yes":
        v2_side = "bid"
        v2_price = min(price_cents + TAKER_BUFFER_CENTS, 99.0) / 100.0
    else:
        v2_side = "ask"
        v2_price = max(100.0 - price_cents - TAKER_BUFFER_CENTS, 1.0) / 100.0
    body = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": v2_side,
        "count": f"{float(count):.2f}",
        "price": f"{v2_price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "reduce_only": reduce_only,
    }
    try:
        resp = await kalshi_post("/portfolio/events/orders", body)
        order_info = resp.get("order", resp) if isinstance(resp, dict) else {}
        raw_filled = order_info.get("fill_count_fp") or order_info.get("fill_count") or "0"
        try:
            filled_f = float(raw_filled)
        except (TypeError, ValueError):
            filled_f = 0.0
        fill_status = order_info.get("status") or order_info.get("fill_status") or (
            "filled" if filled_f > 0 else "not_filled"
        )
        return {"ok": True, "fill_status": str(fill_status), "filled": filled_f, "request": body, "response": resp}
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.text[:400]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {detail}", "request": body}
    except Exception as e:
        return {"ok": False, "error": str(e), "request": body}

# ------------------------------------------------------- take-profit exits ---
async def close_position(pos: dict, reason: str):
    """Exit a position at the current bid as taker. Selling YES at b cents is
    identical to buying NO at (100-b) cents, so we reuse place_order."""
    ticker, side, count = pos["ticker"], pos["side"], pos["count"]
    try:
        data = await kalshi_get(f"/markets/{ticker}")
        m = data.get("market", {})
        if side == "yes":
            bid = cents(m, "yes_bid")
            exit_side, exit_px = "no", (100.0 - bid) if bid is not None else None
        else:
            ask = cents(m, "yes_ask")
            bid = (100.0 - ask) if ask is not None else None
            exit_side, exit_px = "yes", ask
        if bid is None or exit_px is None:
            return False
        order = await place_order(ticker, exit_side, exit_px, count, reduce_only=True)
        if order.get("ok") and float(order.get("filled") or 0) > 0:
            profit = (bid - pos["entry_c"]) * count / 100.0
            await tg_send(
                f"TAKE-PROFIT EXIT\n{ticker}\nclosed {side.upper()} x{count:g} @ {round(bid, 1)}c "
                f"(entry {round(pos['entry_c'], 1)}c)\nlocked: {'+' if profit >= 0 else ''}"
                f"{round(profit, 2)} USD ({reason})")
            return True
    except Exception as e:
        log.error(f"close_position {ticker}: {e}")
    return False

async def monitor_positions():
    """Take-profit watchdog. Runs every scan cycle even while paused:
    pause stops entries, never exits."""
    if TAKE_PROFIT_CENTS <= 0 or not STATE["open_positions"]:
        return
    still_open = []
    for pos in STATE["open_positions"]:
        try:
            data = await kalshi_get(f"/markets/{pos['ticker']}")
            m = data.get("market", {})
            if str(m.get("status", "")).lower() not in ("open", "active", "initialized"):
                continue  # settled/closed: nothing left to manage
            if pos["side"] == "yes":
                bid = cents(m, "yes_bid")
            else:
                ask = cents(m, "yes_ask")
                bid = (100.0 - ask) if ask is not None else None
            if bid is not None and bid >= pos["entry_c"] + TAKE_PROFIT_CENTS:
                if await close_position(pos, f"target +{TAKE_PROFIT_CENTS:g}c"):
                    continue
            still_open.append(pos)
        except Exception as e:
            log.error(f"monitor {pos.get('ticker')}: {e}")
            still_open.append(pos)
    STATE["open_positions"] = still_open

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
        try:
            await monitor_positions()
        except Exception as e:
            log.error(f"position monitor error: {e}")
        await asyncio.sleep(SCAN_INTERVAL_SEC)

# --------------------------------------------- polymarket intelligence ------
POLY_STATE = {
    "reachable": None,
    "last_scan": None,
    "last_error": None,
    "markets_seen": 0,
    "board": [],
    "watched": [],
    "whales": [],
    "gaps": [],
    "seen_trades": set(),
    "gap_alerted_at": {},
}

async def poly_get(url: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, params=params,
                        headers={"User-Agent": "sixfilter-scanner/1.0"})
        r.raise_for_status()
        return r.json()

def _poly_yes_price(m: dict):
    """Gamma returns outcomes/outcomePrices as JSON strings."""
    try:
        import json as _json
        outs = m.get("outcomes"); prices = m.get("outcomePrices")
        if isinstance(outs, str): outs = _json.loads(outs)
        if isinstance(prices, str): prices = _json.loads(prices)
        for name, px in zip(outs or [], prices or []):
            if str(name).strip().lower() == "yes":
                return float(px) * 100.0
        if prices:
            return float(prices[0]) * 100.0
    except Exception:
        pass
    return None

async def poly_scan_once():
    """One full sweep: board + whales + watched themes + pair gaps."""
    markets = await poly_get(f"{POLY_GAMMA}/markets", params={
        "active": "true", "closed": "false", "limit": 60,
        "order": "volume24hr", "ascending": "false"})
    if isinstance(markets, dict):
        markets = markets.get("markets") or markets.get("data") or []
    POLY_STATE["reachable"] = True
    POLY_STATE["markets_seen"] = len(markets)

    board = []
    watched = []
    for m in markets:
        q = m.get("question") or m.get("title") or ""
        yes = _poly_yes_price(m)
        row = {
            "question": q[:110],
            "yes_c": round(yes, 1) if yes is not None else None,
            "vol24h": round(float(m.get("volume24hr") or m.get("volume") or 0)),
            "liquidity": round(float(m.get("liquidity") or 0)),
            "ends": str(m.get("endDate") or m.get("end_date") or "")[:10],
            "slug": m.get("slug") or "",
        }
        board.append(row)
        if any(k in q.lower() or k in row["slug"].lower() for k in POLY_WATCH_KEYWORDS):
            watched.append(row)
    POLY_STATE["board"] = board[:25]
    POLY_STATE["watched"] = watched[:25]

    # --- whale radar ---
    trades = await poly_get(f"{POLY_DATA}/trades", params={"limit": 200})
    if isinstance(trades, dict):
        trades = trades.get("trades") or trades.get("data") or []
    seen = POLY_STATE["seen_trades"]
    for t in trades:
        tid = t.get("transactionHash") or t.get("id") or f"{t.get('timestamp')}{t.get('proxyWallet')}{t.get('size')}"
        if tid in seen:
            continue
        seen.add(tid)
        try:
            usd = float(t.get("size") or 0) * float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if usd < POLY_WHALE_MIN_USD:
            continue
        row = {
            "ts": datetime.fromtimestamp(int(t.get("timestamp", 0)), tz=timezone.utc).strftime("%m-%d %H:%M")
            if str(t.get("timestamp", "")).isdigit() else "?",
            "usd": round(usd),
            "side": t.get("side") or "?",
            "price_c": round(float(t.get("price") or 0) * 100, 1),
            "title": (t.get("title") or t.get("market") or "?")[:90],
            "outcome": t.get("outcome") or "?",
            "wallet": str(t.get("proxyWallet") or "")[:10],
        }
        POLY_STATE["whales"].insert(0, row)
        await tg_send(
            f"POLY WHALE ${row['usd']:,}\n"
            f"{row['side']} {row['outcome']} @ {row['price_c']}c\n"
            f"{row['title']}\n"
            f"wallet {row['wallet']}... | {row['ts']} UTC")
    POLY_STATE["whales"] = POLY_STATE["whales"][:25]
    if len(seen) > 5000:
        POLY_STATE["seen_trades"] = set(list(seen)[-2000:])

    # --- verified pair gaps (manual pairs only, by design) ---
    for sub, ticker in POLY_KALSHI_PAIRS:
        match = next((m for m in markets
                      if sub in (m.get("question") or "").lower()
                      or sub in (m.get("slug") or "").lower()), None)
        if match is None:
            try:
                res = await poly_get(f"{POLY_GAMMA}/markets",
                                     params={"slug": sub, "limit": 1})
                if isinstance(res, list) and res:
                    match = res[0]
            except Exception:
                pass
        if match is None:
            continue
        poly_yes = _poly_yes_price(match)
        try:
            km = (await kalshi_get(f"/markets/{ticker}")).get("market", {})
            kbid, kask = cents(km, "yes_bid"), cents(km, "yes_ask")
            kalshi_mid = (kbid + kask) / 2.0 if kbid is not None and kask is not None else None
        except Exception as e:
            log.warning(f"pair gap kalshi fetch {ticker}: {e}")
            continue
        if poly_yes is None or kalshi_mid is None:
            continue
        gap = poly_yes - kalshi_mid
        row = {"pair": f"{sub}:{ticker}", "poly_c": round(poly_yes, 1),
               "kalshi_c": round(kalshi_mid, 1), "gap_c": round(gap, 1),
               "ts": datetime.now(timezone.utc).strftime("%m-%d %H:%M")}
        POLY_STATE["gaps"].insert(0, row)
        last = POLY_STATE["gap_alerted_at"].get(row["pair"], 0)
        if abs(gap) >= POLY_GAP_ALERT_C and time.time() - last > 3600:
            POLY_STATE["gap_alerted_at"][row["pair"]] = time.time()
            cheaper = "KALSHI" if gap > 0 else "POLY"
            await tg_send(
                f"POLY-KALSHI GAP {abs(gap):.1f}c\n"
                f"{(match.get('question') or sub)[:80]}\n"
                f"Polymarket YES {poly_yes:.1f}c / Kalshi mid {kalshi_mid:.1f}c\n"
                f"cheaper side: {cheaper} - CHECK RESOLUTION TERMS FIRST")
    POLY_STATE["gaps"] = POLY_STATE["gaps"][:25]
    POLY_STATE["last_scan"] = datetime.now(timezone.utc).isoformat()

async def poly_loop():
    await asyncio.sleep(15)
    if not POLY_ENABLED:
        log.info("polymarket scanner disabled (POLY_ENABLED=false)")
        return
    try:
        await poly_get(f"{POLY_GAMMA}/markets", params={"limit": 1})
        POLY_STATE["reachable"] = True
        log.info("POLYMARKET: reachable - scanner online")
        await tg_send("Polymarket scanner online (read-only). Whale radar + gap watch active.")
    except Exception as e:
        POLY_STATE["reachable"] = False
        POLY_STATE["last_error"] = str(e)
        log.error(f"POLYMARKET: UNREACHABLE from this host: {e}")
        await tg_send(f"Polymarket scanner CANNOT reach Polymarket from Railway: {e}")
        return
    while True:
        try:
            await poly_scan_once()
        except Exception as e:
            POLY_STATE["last_error"] = str(e)
            log.error(f"poly scan error: {e}")
        await asyncio.sleep(POLY_SCAN_SEC)

# -------------------------------------------------------------------- app ---
app = FastAPI(title="SixFilter Kalshi Trader API", docs_url="/docs")

@app.on_event("startup")
async def _startup():
    load_key()
    asyncio.create_task(auto_loop())
    asyncio.create_task(poly_loop())

# ------------------------------------------- offline model calibration test ---
async def _fetch_klines(symbol: str, days: int):
    """Pull `days` of 1m closes from Binance (1000-bar pages, walking back)."""
    closes = []
    end = int(time.time() * 1000)
    need = days * 1440
    while len(closes) < need:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://api.binance.com/api/v3/klines", params={
                "symbol": symbol, "interval": "1m",
                "limit": min(1000, need - len(closes)), "endTime": end - 1})
            r.raise_for_status()
            ks = r.json()
            if not ks:
                break
            closes = [float(k[4]) for k in ks] + closes
            end = int(ks[0][0])
        await asyncio.sleep(0.15)  # be polite with rate limits
    return closes

@app.get("/backtest")
async def backtest(symbol: str = "BTCUSDT", days: int = 7, adj: int = 0):
    """Replay the EXACT production model over historical 15-min windows.
    Calibration: when the model says p, does price close above the window
    open p-fraction of the time? adj=1 applies the production recalibrate().
    Read-only."""
    days = max(1, min(int(days), 14))
    symbol = symbol.upper()
    closes = await _fetch_klines(symbol, days)
    out = {"symbol": symbol, "days": days, "bars": len(closes)}
    if len(closes) < 500:
        out["error"] = "not enough kline data"
        return out
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

    WINDOW = 15
    report = {}
    for m_left in (13, 8, 4):
        buckets = [[0, 0.0, 0.0] for _ in range(10)]  # [n, p_sum, wins]
        mom = {"big_up": [0, 0.0, 0.0], "small": [0, 0.0, 0.0], "big_down": [0, 0.0, 0.0]}
        brier = brier_naive = 0.0
        n = 0
        t_eff = max(m_left - 0.5, 0.25)  # production settlement adjustment
        for d in range(125, len(closes) - m_left, 3):
            hist = rets[d - 120:d]  # same 120 bars the bot reads
            sigma = statistics.pstdev(hist) if len(hist) > 2 else 0.001
            drift = statistics.mean(hist[-20:]) * 0.15  # production drift weight
            spot = closes[d]
            strike = closes[d - (WINDOW - m_left)]  # price at window open
            win = 1.0 if closes[d + m_left] >= strike else 0.0
            p = prob_above(spot, strike, sigma, drift, t_eff)
            if adj:
                r15_bt = math.log(closes[d] / closes[d - WINDOW])
                p = recalibrate(p, m_left, r15_bt, sigma, symbol)
            b = min(int(p * 10), 9)
            buckets[b][0] += 1; buckets[b][1] += p; buckets[b][2] += win
            brier += (p - win) ** 2; brier_naive += (0.5 - win) ** 2
            r_prev = math.log(closes[d] / closes[d - WINDOW])
            big = 1.5 * sigma * math.sqrt(WINDOW)
            key = "big_up" if r_prev > big else ("big_down" if r_prev < -big else "small")
            mom[key][0] += 1; mom[key][1] += win; mom[key][2] += p
            n += 1
        report[f"minutes_left_{m_left}"] = {
            "samples": n,
            "brier_model": round(brier / n, 4) if n else None,
            "brier_coinflip": round(brier_naive / n, 4) if n else None,
            "calibration": [
                {"range": f"{i / 10:.1f}-{(i + 1) / 10:.1f}", "n": bk[0],
                 "model_avg": round(bk[1] / bk[0], 3), "actual_freq": round(bk[2] / bk[0], 3)}
                for i, bk in enumerate(buckets) if bk[0]
            ],
            "momentum_split": {
                k: {"n": v[0],
                    "actual_up_freq": round(v[1] / v[0], 3) if v[0] else None,
                    "model_avg_p": round(v[2] / v[0], 3) if v[0] else None}
                for k, v in mom.items()
            },
        }
    out["results"] = report
    out["how_to_read"] = ("calibration: model_avg should roughly equal actual_freq in every "
                          "bucket. momentum_split: if big_up actual_up_freq sits well below "
                          "model_avg_p, the model overprices trend continuation -> mean "
                          "reversion confirmed and sized.")
    return out

# ------------------------------------------- PATCH 4: strategy P&L replay ---
@app.get("/backtest/pnl")
async def backtest_pnl(symbol: str = "BTCUSDT", days: int = 7,
                       min_price: float = 55.0, max_price: float = 90.0,
                       edge: float = 0.08, fee: int = 1):
    """Replay the STRATEGY (not just the model) over historical windows.

    The /backtest endpoint answers "is the model calibrated?" This one answers
    "does the 6-filter gate make money?" For each historical 15m window where
    the model shows edge >= threshold, we simulate buying YES at the model-
    implied market price band and settling at the true outcome, with Kalshi
    taker fees. Since we cannot see historical Kalshi books, we approximate the
    market price as the model probability adjusted by a spread assumption
    (spread=2c each side, matching observed KX*15M books). fee=1 includes fees.

    Read-only. Compare min_price=48 vs 55 to see the PATCH 1 effect directly.
    """
    days = max(1, min(int(days), 14))
    symbol = symbol.upper()
    closes = await _fetch_klines(symbol, days)
    out = {"symbol": symbol, "days": days, "bars": len(closes),
           "params": {"min_price": min_price, "max_price": max_price,
                      "edge": edge, "fees_included": bool(fee)}}
    if len(closes) < 500:
        out["error"] = "not enough kline data"
        return out
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

    WINDOW = 15
    SPREAD_C = 2.0  # assumed half-spread on KX 15m books
    report = {}
    for m_left in (13, 8, 4):
        trades = []
        t_eff = max(m_left - 0.5, 0.25)
        for d in range(125, len(closes) - m_left, 3):
            hist = rets[d - 120:d]
            sigma = statistics.pstdev(hist) if len(hist) > 2 else 0.001
            drift = statistics.mean(hist[-20:]) * 0.15
            spot = closes[d]
            strike = closes[d - (WINDOW - m_left)]
            win = closes[d + m_left] >= strike
            p = prob_above(spot, strike, sigma, drift, t_eff)
            r15_bt = math.log(closes[d] / closes[d - WINDOW])
            p = recalibrate(p, m_left, r15_bt, sigma, symbol)

            # approximate market: mid ~= p (efficient) -> tradeable YES ask ~= p*100 + spread
            yes_ask = p * 100.0 + SPREAD_C
            yes_bid = p * 100.0 - SPREAD_C
            mid = (yes_ask + yes_bid) / 2.0
            if not (min_price <= yes_ask <= max_price):
                continue
            edge_yes = p - yes_ask / 100.0
            edge_no = (yes_bid / 100.0) - p
            if edge_yes >= edge_no and edge_yes >= edge and mid >= 50:
                side, entry_c = "yes", yes_ask
            elif edge_no > edge_yes and edge_no >= edge and mid < 50:
                side, entry_c = "no", 100.0 - yes_bid
            else:
                continue
            if abs(p - 0.5) < 0.02:  # clarity filter
                continue
            fee_c = kalshi_taker_fee_cents(entry_c) if fee else 0.0
            if side == "yes":
                pnl = (100.0 - entry_c) if win else -entry_c
            else:
                pnl = (100.0 - entry_c) if not win else -entry_c
            pnl -= fee_c
            trades.append(pnl)

        n = len(trades)
        if n:
            eq, peak, maxdd = 0.0, 0.0, 0.0
            for x in trades:
                eq += x
                peak = max(peak, eq)
                maxdd = min(maxdd, eq - peak)
            report[f"minutes_left_{m_left}"] = {
                "trades": n,
                "win_rate": round(sum(1 for x in trades if x > 0) / n, 3),
                "total_pnl_dollars": round(sum(trades) / 100.0, 2),
                "avg_pnl_cents": round(sum(trades) / n, 2),
                "max_drawdown_dollars": round(maxdd / 100.0, 2),
                "profit_factor": round(
                    sum(x for x in trades if x > 0) / max(0.01, -sum(x for x in trades if x < 0)), 2),
            }
        else:
            report[f"minutes_left_{m_left}"] = {"trades": 0}
    out["results"] = report
    out["how_to_read"] = ("This is a lower-fidelity replay (synthetic books at model price "
                          "+/- 2c spread) - use it to compare SETTINGS, not to predict exact "
                          "returns. Try min_price=48 vs 55 vs 58 and watch total_pnl move. "
                          "If profit_factor < 1.3 in replay, do not trade that config live.")
    return out

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
        "min_price_cents": MIN_PRICE_CENTS,
        "fill_floor_cents": FILL_FLOOR_CENTS,
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
        "spent_today_dollars": round(STATE["spent_today_cents"] / 100.0, 2),
        "daily_loss_limit": DAILY_LOSS_LIMIT,
        "edge_threshold": EDGE_THRESHOLD,
        "trade_size": TRADE_SIZE,
        "min_price_cents": MIN_PRICE_CENTS,
        "fill_floor_cents": FILL_FLOOR_CENTS,
        "scanning": SCAN_SERIES,
        "take_profit_cents": TAKE_PROFIT_CENTS,
        "open_positions": [
            {"ticker": p["ticker"], "side": p["side"], "count": p["count"], "entry_c": p["entry_c"]}
            for p in STATE["open_positions"]
        ],
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
    side: str  # "yes" or "no"
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
        filled_n = float(order.get("filled") or 0)
        if filled_n > 0:
            reset_daily()
            STATE["trades_today"] += 1
            STATE["spent_today_cents"] += (price or 0) * min(filled_n, float(req.count))
            STATE["traded_tickers"].append(req.ticker)
            STATE["open_positions"].append({
                "ticker": req.ticker, "side": req.side.lower(),
                "count": min(filled_n, float(req.count)),
                "entry_c": float(price), "ts": time.time()})
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
DASH_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SixFilter Kalshi</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;max-width:900px;margin:0 auto;padding:16px}
h1{color:#f59e0b;font-size:1.3rem}
.card{background:#1e293b;border-radius:10px;padding:14px;margin:10px 0}
.sig{font-family:monospace;font-size:.8rem;white-space:pre-wrap;background:#0b1220;padding:10px;border-radius:8px}
.ok{color:#4ade80}.bad{color:#f87171}.dim{color:#94a3b8}
button{background:#f59e0b;border:none;border-radius:8px;padding:8px 14px;font-weight:600;margin-right:8px;cursor:pointer}
</style></head><body>
<h1>SixFilter Kalshi Trader</h1>
<div class="dim">auto-refreshes every 10s</div>
<div class="card" id="s">loading...</div>
<div class="card">
<button onclick="act('/admin/resume')">Resume</button>
<button onclick="act('/admin/pause')">Pause</button>
<button onclick="act('/scan','POST',true)">Scan Now</button>
</div>
<div class="card"><b>Last scan signals</b><div class="sig" id="sig">...</div></div>
<script>
async function act(url, method='POST', reload=false){
  await fetch(url,{method,headers:{'Content-Type':'application/json'},body:method==='POST'?'{}':undefined});
  if(reload) load();
}
async function load(){
  try{
    const s = await (await fetch('/status')).json();
    document.getElementById('s').innerHTML =
      `<span class="${s.paused?'bad':'ok'}">${s.paused?'PAUSED':'ACTIVE'}</span> · ` +
      `trades today: <b>${s.trades_today}/${s.max_trades_per_day}</b> · ` +
      `spent: $${s.spent_today_dollars} · floor: ${s.min_price_cents}c/${s.fill_floor_cents}c<br>` +
      `<span class="dim">scanning: ${s.scanning.join(', ')}<br>last scan: ${s.last_scan||'-'}<br>` +
      (s.last_error?`<span class="bad">error: ${s.last_error}</span>`:'') + `</span>`;
    document.getElementById('sig').textContent =
      (s.last_signals||[]).map(r=>`${r.series}: ${r.side||'-'} edge=${r.edge??'-'} ${r.reason||''}`).join('\n') || 'no scans yet';
  }catch(e){ document.getElementById('s').textContent = 'dashboard error: '+e; }
}
load(); setInterval(load, 10000);
</script></body></html>"""

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return DASH_HTML

# ------------------------------------------------------------------ debug ---
@app.get("/debug/markets")
async def debug_markets(series: str = "KXBTC15M"):
    data = await kalshi_get("/markets", params={"series_ticker": series, "limit": 200})
    mkts = [m for m in data.get("markets", []) if (m.get("status") or "").lower() in ("open", "initialized", "active")]
    now = time.time()

    def close_ts(m):
        exp = m.get("close_time") or m.get("expiration_time")
        try:
            return datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp()
        except Exception:
            return float("inf")

    mkts = sorted(mkts, key=close_ts)  # soonest-closing first, like the scanner
    out = []
    for m in mkts[:5]:
        exp = m.get("close_time") or m.get("expiration_time")
        mins = None
        try:
            mins = round((datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp() - now) / 60, 1)
        except Exception:
            pass
        out.append({
            "ticker": m.get("ticker"),
            "status": m.get("status"),
            "close_time": m.get("close_time"),
            "expiration_time": m.get("expiration_time"),
            "mins_to_close": mins,
            "yes_bid": m.get("yes_bid"),
            "yes_ask": m.get("yes_ask"),
            "floor_strike": m.get("floor_strike"),
            "strike_type": m.get("strike_type"),
        })
    return {"count": len(mkts), "markets": out}

@app.get("/debug/market")
async def debug_market(ticker: str):
    """Full raw Kalshi data for ONE market - lifecycle forensics."""
    data = await kalshi_get(f"/markets/{ticker}")
    m = data.get("market", data)
    now = time.time()
    close = m.get("close_time")
    mins = None
    try:
        mins = round((datetime.fromisoformat(str(close).replace("Z", "+00:00")).timestamp() - now) / 60, 1)
    except Exception:
        pass
    return {
        "ticker": m.get("ticker"),
        "status": m.get("status"),
        "open_time": m.get("open_time"),
        "close_time": close,
        "expiration_time": m.get("expiration_time"),
        "mins_to_close": mins,
        "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
        "no_bid": m.get("no_bid"), "no_ask": m.get("no_ask"),
        "last_price": m.get("last_price"),
        "volume": m.get("volume"), "open_interest": m.get("open_interest"),
        "floor_strike": m.get("floor_strike"), "cap_strike": m.get("cap_strike"),
        "strike_type": m.get("strike_type"),
        "can_close_early": m.get("can_close_early"),
        "raw": m,
    }

@app.get("/poly")
def poly_status():
    """Polymarket scanner state (JSON)."""
    return {
        "enabled": POLY_ENABLED,
        "reachable": POLY_STATE["reachable"],
        "last_scan": POLY_STATE["last_scan"],
        "last_error": POLY_STATE["last_error"],
        "markets_seen": POLY_STATE["markets_seen"],
        "whale_min_usd": POLY_WHALE_MIN_USD,
        "gap_alert_c": POLY_GAP_ALERT_C,
        "watch_keywords": POLY_WATCH_KEYWORDS,
        "pairs": [f"{a}:{b}" for a, b in POLY_KALSHI_PAIRS],
        "whales": POLY_STATE["whales"],
        "gaps": POLY_STATE["gaps"],
        "watched": POLY_STATE["watched"],
        "board": POLY_STATE["board"],
    }

@app.get("/poly/board", response_class=HTMLResponse)
def poly_board():
    """Mobile-friendly Polymarket intel page - open from your phone."""
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    reach = POLY_STATE["reachable"]
    badge = ("REACHABLE" if reach else
             "UNREACHABLE" if reach is False else
             "PROBING...")

    def tbl(rows, headers):
        if not rows:
            return "<p class='dim'>none yet</p>"
        h = "".join(f"<th>{x}</th>" for x in headers)
        body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
        return f"<table><tr>{h}</tr>{body}</table>"

    whales = tbl([[f"${w['usd']:,}", esc(w['side']), f"{w['price_c']}c", esc(w['title']), w['ts']]
                  for w in POLY_STATE["whales"]], ["Size", "Side", "Px", "Market", "UTC"])
    gaps = tbl([[esc(g['pair']), f"{g['poly_c']}c", f"{g['kalshi_c']}c", f"{g['gap_c']:+}c", g['ts']]
                for g in POLY_STATE["gaps"]], ["Pair", "Poly YES", "Kalshi mid", "Gap", "UTC"])
    watched = tbl([[esc(m['question']), f"{m['yes_c']}c", f"${m['vol24h']:,}", m['ends']]
                   for m in POLY_STATE["watched"]], ["Market", "YES", "24h Vol", "Ends"])
    board = tbl([[esc(m['question']), f"{m['yes_c']}c", f"${m['vol24h']:,}", m['ends']]
                 for m in POLY_STATE["board"]], ["Market", "YES", "24h Vol", "Ends"])

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120"><title>Polymarket Intel</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;max-width:900px;margin:0 auto;padding:16px}}
h1{{color:#f59e0b;font-size:1.3rem}} h2{{font-size:1rem;color:#cbd5e1}}
table{{width:100%;border-collapse:collapse;font-size:.8rem}}
td,th{{padding:6px 8px;border-bottom:1px solid #334155;text-align:left}}
.dim{{color:#94a3b8}}
</style></head><body>
<h1>Polymarket Scanner &nbsp; <small>{badge}</small></h1>
<p class="dim">last scan: {POLY_STATE['last_scan'] or 'never'} · markets seen: {POLY_STATE['markets_seen']} ·
whale threshold: ${POLY_WHALE_MIN_USD:,.0f} · error: {esc(POLY_STATE['last_error'] or '-')} · read-only, refreshes every 2 min</p>
<h2>Whale prints (last 25)</h2>{whales}
<h2>Verified Kalshi pairs - gap watch</h2>{gaps}
<h2>Watched themes ({', '.join(POLY_WATCH_KEYWORDS)})</h2>{watched}
<h2>Top markets by 24h volume</h2>{board}
</body></html>"""

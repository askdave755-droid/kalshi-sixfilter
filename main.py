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
7. Momentum module (Aug 9, 2026) - three parts, all tunable via env:
   a) ALIGNMENT (MOM_K, MOM_ALIGN_MINUTES): when the trailing 15m move
      exceeds MOM_K * sigma_15, refuse to trade INTO momentum late in the
      window (no YES into a falling tape, no NO into a rising one, when
      mins < MOM_ALIGN_MINUTES). Aug 8 PM session: falling-tape YES buys
      went 35% WR / -$3.81.
   b) OVERRIDE (MOM_OVERRIDE_P, MOM_OVERRIDE_EDGE, MOM_OVERRIDE_BAND,
      MOM_OVERRIDE_MIN_MINUTES): when model + momentum agree strongly
      (down tape + p <= 0.40, or up tape + p >= 0.60) with >= 5 min left,
      the Consensus Gate's neutral band relaxes from mid>=50 / mid<50 to
      mid 48-52, but the stricter MOM_OVERRIDE_EDGE floor applies inside
      that band. Ships in prove-it mode: override fills are tagged
      [MOM OVERRIDE] in Telegram and will be graded separately.
   c) LOGGING: every order + evaluation now carries r15, mid, momentum,
      and override flags so the next CSV backtest can grade the patch
      itself instead of inferring regime from outcomes.
7b. Momentum v2 (Aug 9, falsified overnight at n=50): override is now
   momentum-LED (tape sets direction, model only must not disagree at
   p<0.55) instead of model-led (p<=0.40 never fired). New 45m session
   trend blocks YES/NO into grinds at ANY minute in the window. Momentum
   threshold has an absolute floor (MOM_FLOOR_PCT) so dead-vol noise
   stops getting labeled up/down. Crash-fill guard now blacklists the
   ticker for the day (no re-buying the knife). /status self-reports
   max_price_cents + full momentum config.
7c. Startup rehydration (Aug 9): boot pulls today's fills from the ledger
   and rebuilds trades_today / spent_today_cents / traded_tickers, so a
   redeploy can no longer reset the daily trade + spend caps (observed:
   25/25 filled, redeploy, fresh 25-trade budget). Loud Telegram alert on
   rehydrate success or failure.
7d. Execution-seam fixes (Aug 9): Guard 1 now re-checks the PRICE BAND at
   order time (was consensus-only - the 74c over-cap fill and 54c sub-floor
   slide both slipped through a moved quote). Rehydrate reads dollar-encoded
   price fields (fixes $0.0-spent bug). Crash-fill guard logs every verified
   fill price and screams if the dump order itself fails (was silent - a 54c
   fill rode to settlement).
7e. Rehydrate hotfixes (Aug 9): stamp STATE["day"] at boot so reset_daily
   can't wipe the rebuilt counters on the first scan (45/25 -> traded again
   2 min later), and read count_fp for spend (new API field - fixes the
   persistent $0.0-spent bug).
8. Trading-hours whitelist (Aug 10): full-week ledger calibration (n=363)
   shows the edge only exists in UTC hours 1,2,5,6,9,12,13,21,23
   (hour 22 = PF 0.26; replay of whitelist + 55-75c band = PF 2.13 vs
   0.85 actual). Filter 0 in analyze_series skips all other hours with
   the reason logged; TRADING_HOURS_UTC env overrides; /status reports it.
   Ships with MAX_PRICE_CENTS raised to 75 (70-75c band = PF 2.33, the
   best slice) - set that in Railway Variables alongside this deploy.
9. Backtest v2 data export (Aug 10, READ-ONLY): /export/start|status|
   download pulls settled 15M markets + 1m candlesticks (live tier, ~3mo
   window) into /tmp/bt_export NDJSON for offline replay against REAL
   quotes. Guarded by EXPORT_KEY env. No orders, no money.
10. Maker mode (Aug 10): backtest v2 (30d, 5,688 markets, real quotes)
   showed identical signals lose as taker (PF 0.93, -$18) but profit as
   maker (PF 1.05, +$13) - friction, not signal, was the leak. MAKER_MODE=1
   posts resting post_only GTC bids at the live quote (0 maker fee),
   MAKER_TTL_SEC (default 90) cancels unfilled orders; fills consume daily
   caps like taker fills; crash-fill guard + blacklist apply; startup
   sweep cancels strays after restarts. Off by default (env-gated).
11. Export strike-kind filter (kind=greater|less|all) for daily-market pulls.
12. SOL engine (Aug 14): KXSOL15M mapped to SOLUSDT. 30d replay through the
   exact production stack (maker mode, hours whitelist, 55-75c band):
   296 trades, 65.5% WR, PF 1.15, +$9.27 (XRP failed: PF 0.82, skipped).
   Per-series sizing via SERIES_SIZE / size_for() - SOL trades its own size
   (SIZE_KXSOL15M env, default 1) independent of CONTRACT_SIZE. Requires
   KXSOL15M added to SCAN_SERIES in Railway Variables.
13. Anti-martingale press experiment (Aug 15, SOL only): maker-era fills
   showed wins cluster (17-streak; win->2/loss->1 would have made +$21 vs
   +$9 flat on identical trades). update_press_state() settles press-series
   positions each scan: win presses next size +1 (cap PRESS_MAX, default 2),
   loss resets to base. CLOSED Aug 17: SOL wins don't cluster (press cost
   -$0.94 vs flat over 3 presses); PRESS_SERIES=OFF in production.
14. Econ scanner v2 (Aug 17): v1's "gaps" were artifacts - it answered
   threshold questions ("CPI above 6%?") with bracket-mass lookups and fed
   YoY questions a month-over-month distribution. v2 keeps raw FRED samples
   (ECON_DIST) and answers P(dist >= x) / P(dist < x) via empirical CDF
   (static normal fallback); claims/CPI parse above-below thresholds; YoY vs
   m/m detected per market. Paper ledger now persists to
   /tmp/econ_paper.ndjson and rehydrates on boot - redeploys no longer wipe
   the record. Still PAPER ONLY - no orders.
15. Polymarket measurement layer (Aug 17): user's Poly jurisdiction solved.
   Before any build, we measure: poly_scan_once now inventories Poly crypto
   up/down markets (POLY_STATE["crypto_pm"]) and logs candidate gaps vs
   Kalshi 15M mids (POLY_STATE["cand_gaps"], alert >= POLY_GAP_ALERT_C,
   1h dedupe, tagged UNVERIFIED TERMS - resolution terms auto-matched by
   theme, never trusted for money without a manual check). Both surfaces
   exposed in /poly. Read-only; no Poly execution exists yet.
16. Scan journal (Aug 17): the tape recorder. Every scan appends one NDJSON
   line per series to /tmp/scan_journal.ndjson - quote, model p, edge,
   r15/r45, hour, minutes-to-close, decision AND rejection reason. Losses can
   now be joined back to the minutes BEFORE entry to see what a bad
   environment looks like in motion. /journal (tail) + /journal/download
   (key-guarded). Read-only; trading logic untouched. Resets on redeploy
   like the econ paper ledger - pull it down before patching.
17. Journal paging + download fix (Aug 21): /journal?n=..&offset=.. pages the
   whole tape (default stays tail). /journal/download now serves the file
   inline instead of FileResponse, which stalled through the proxy on
   multi-MB files. Read-only; trading logic untouched.
18. HEAD repair (Aug 23): removed an empty duplicate root() stub left by an
   Aug-22 web edit - it crashed the service with IndentationError on any
   rebuild from HEAD. No logic changes.
"""

import os
import re
import json
import time
import math
import base64
import asyncio
import logging
import statistics
import uuid
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
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
    "KXSOL15M": "SOLUSDT",  # PATCH 12: SOL engine (30d replay PF 1.15, 65.5% WR)
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
# PATCH 12: per-series size overrides. SOL runs its own size (default 1)
# while BTC/ETH keep CONTRACT_SIZE. Env: SIZE_KXSOL15M etc.
SERIES_SIZE = {"KXSOL15M": env_int("SIZE_KXSOL15M", "SOL_SIZE", default=1)}
# PATCH 13: anti-martingale press experiment, SOL only. Live A/B vs flat-1:
# maker-era data showed wins cluster (17-streak; win->2 would have doubled
# P&L on identical fills). Win -> size 2, loss -> back to 1, never above
# PRESS_MAX. BTC/ETH untouched. State resets to base size on redeploy
# (conservative). PRESS_SERIES env to extend/disable ("" = off).
PRESS_SERIES = {s.strip().upper() for s in env(
    "PRESS_SERIES", default="KXSOL15M").split(",") if s.strip()}
PRESS_MAX = env_int("PRESS_MAX", default=2)
def size_for(series: str) -> int:
    if series in PRESS_SERIES:
        base = SERIES_SIZE.get(series, 1)
        return max(1, min(PRESS_MAX, STATE["press"].get(series, base)))
    return SERIES_SIZE.get(series, TRADE_SIZE)
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

# PATCH 7 — Momentum module (built on the 7-day, 13k-window backtest):
# after big 15m moves, continuation runs 80-96% with 4-8min left (mean
# reversion is a myth at this horizon). Two mechanisms:
# 7a) ALIGNMENT BLOCK: late in the window, refuse to trade INTO momentum
#     (no YES while tape is falling, no NO while it's rising). This kills
#     the boundary "knife-catch" trades that went 35% WR on Aug 8 PM.
# 7b) MOMENTUM OVERRIDE: when model + momentum agree strongly, relax the
#     consensus gate's neutral band (mid 48-52) so the bot can actually
#     take the NO side as momentum shifts - but demand a bigger edge
#     (MOM_OVERRIDE_EDGE) as the safety payment for overriding the gate.
MOM_K = env_float("MOM_K", default=0.5)               # threshold = K * sigma_15m
MOM_ALIGN_MINUTES = env_float("MOM_ALIGN_MINUTES", default=10.0)  # block zone: mins < this
MOM_OVERRIDE_P = env_float("MOM_OVERRIDE_P", default=0.40)        # model must be <= this (NO) / >= 1-this (YES)
MOM_OVERRIDE_EDGE = env_float("MOM_OVERRIDE_EDGE", default=0.12)  # stricter edge inside neutral band
MOM_OVERRIDE_BAND = env_float("MOM_OVERRIDE_BAND", default=2.0)   # cents of neutral band past 50
MOM_OVERRIDE_MIN_MINUTES = env_float("MOM_OVERRIDE_MIN_MINUTES", default=5.0)

# PATCH 7b (Aug 9, 2026) - momentum module v2. Falsified overnight at n=50:
# the model stayed p=0.75-0.99 while the tape bled, so the p<=0.40 override
# was unreachable; grind-downs of -0.01..-0.06% per window slipped under the
# 15m threshold; entries at 10-14m sailed past the alignment window; and the
# crash-fill guard's ticker got re-bought. Fixes below, all env-tunable.
MOM_FLOOR_PCT = env_float("MOM_FLOOR_PCT", default=0.0003)      # min threshold: kills noise labels in dead vol
MOM_SESSION_MINUTES = env_float("MOM_SESSION_MINUTES", default=45.0)  # session-trend lookback
MOM_SESSION_K = env_float("MOM_SESSION_K", default=0.5)         # session threshold = K * sigma_session
MOM_OVERRIDE_PMAX = env_float("MOM_OVERRIDE_PMAX", default=0.55)  # model only must NOT disagree (was <=0.40)

TRADING_HOURS_UTC = {int(w.strip()) for w in env(
    "TRADING_HOURS_UTC", default="1,2,5,6,9,12,13,21,23"
).split(",") if w.strip()}

# PATCH 10: maker mode. Post resting bids (0 fee, collect spread) instead of
# crossing the spread as taker. Backtest v2 (30d, real quotes): taker PF 0.93
# (-$18) vs maker PF 1.05 (+$13) on IDENTICAL signals - friction was the leak.
MAKER_MODE = env_int("MAKER_MODE", default=0)          # 1 = post resting bids
MAKER_TTL_SEC = env_float("MAKER_TTL_SEC", default=90.0)  # cancel if unfilled

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

# ------------------------------------------------- econ base-rate scanner ----
# PAPER-ONLY. Discovers Kalshi econ markets (FOMC / CPI / jobless claims),
# prices them against historical base rates, logs a paper trade whenever the
# market diverges, and grades it at settlement. Places NO orders - the v1
# crypto engine was falsified at n=254 (61.4% vs 63.1% breakeven); any v2
# earns order code only after a paper sample proves edge. Same falsification
# gates: measure first, size never until proven.
ECON_ENABLED = env("ECON_ENABLED", default="true").lower() in ("1", "true", "yes")
ECON_SCAN_SEC = env_int("ECON_SCAN_SEC", default=3600)
ECON_GAP_ALERT = env_float("ECON_GAP_ALERT", default=0.10)   # 10 pts divergence
ECON_KEYWORDS = [w.strip().lower() for w in env(
    "ECON_KEYWORDS", default="fed,fomc,cpi,inflation,jobless,claims,payroll,employment situation"
).split(",") if w.strip()]
ECON_MAX_SERIES = env_int("ECON_MAX_SERIES", default=8)

# Historical base rates (approximate, 2021-2026, v1 - refine as the paper log
# teaches us; these are the numbers the THESIS says retail underweights).
BASE_FOMC = {      # outcome of a scheduled meeting
    "hold": 0.65, "cut25": 0.20, "hike25": 0.12, "cut50": 0.02, "hike50": 0.01,
}
BASE_CLAIMS = [    # (upper_bound_K, probability) weekly initial claims, SA
    (200, 0.03), (215, 0.10), (230, 0.25), (245, 0.28),
    (260, 0.18), (275, 0.10), (300, 0.04), (10**9, 0.02),
]
BASE_CPI = [       # (upper_bound_m/m_pct, probability) CPI all-items SA
    (0.0, 0.08), (0.1, 0.10), (0.2, 0.18), (0.3, 0.24),
    (0.4, 0.18), (0.5, 0.12), (0.6, 0.06), (10**9, 0.04),
]

# PATCH 14 (Aug 17): econ scanner v2. v1 compared bracket-mass tables against
# threshold questions ("above 6%?" got "P(in bracket 5.5-6.0)") and measured
# YoY questions against a MONTH-OVER-MONTH distribution - the "gaps" were
# artifacts. v2 keeps raw empirical samples and answers P(dist >= x) /
# P(dist < x) directly. Static normals are the no-FRED fallback.
ECON_DIST = {"claims": [], "cpi_mm": [], "cpi_yoy": []}
ECON_STATIC = {  # (mean, std) normal fallbacks, ~5y history shape
    "claims": (220.0, 25.0),    # thousands, weekly initial claims
    "cpi_mm": (0.25, 0.30),     # m/m %
    "cpi_yoy": (3.5, 2.2),      # y/y % (includes the 21-22 spike)
}

def _norm_cdf(x, mu, sd):
    return 0.5 * (1.0 + math.erf((x - mu) / (sd * math.sqrt(2.0))))

def dist_cdf(kind: str, x: float) -> float:
    """P(dist < x): empirical CDF over FRED samples, static normal fallback."""
    samples = ECON_DIST.get(kind) or []
    if len(samples) >= 20:
        return sum(1 for v in samples if v < x) / len(samples)
    mu, sd = ECON_STATIC[kind]
    return _norm_cdf(x, mu, sd)

# FRED (Federal Reserve Economic Data) - free official API. When FRED_API_KEY
# is set, the static tables above are REPLACED at startup (and daily) with
# base rates computed from real history: ICSA weekly claims + CPIAUCSL m/m.
# FOMC outcomes are decisions, not a series - those stay static for now.
FRED_API_KEY = env("FRED_API_KEY")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

async def fred_series(series_id: str, limit: int):
    """Newest `limit` observations of a FRED series as floats (oldest first)."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(FRED_BASE, params={
            "series_id": series_id, "api_key": FRED_API_KEY,
            "file_type": "json", "sort_order": "desc", "limit": limit})
        r.raise_for_status()
        obs = r.json().get("observations", [])
    vals = []
    for o in reversed(obs):
        try:
            vals.append(float(o["value"]))
        except (KeyError, ValueError):
            pass          # FRED uses "." for missing
    return vals

def _compute_brackets(vals, bounds):
    """Share of observations falling in each (prev, ub] bracket."""
    n = len(vals)
    if n < 20:
        return None
    out, prev = [], 0.0
    for ub in bounds:
        cnt = sum(1 for v in vals if prev <= v < ub)
        out.append((ub, round(cnt / n, 4)))
        prev = ub
    return out

async def refresh_base_rates():
    """Recompute BASE_CLAIMS / BASE_CPI from FRED history. On any failure the
    static defaults stay in place - never block the scanner on the Fed."""
    global BASE_CLAIMS, BASE_CPI
    if not FRED_API_KEY:
        log.info("FRED_API_KEY not set - using static base-rate tables")
        return
    try:
        claims = [v / 1000.0 for v in await fred_series("ICSA", 260)]   # ~5y weekly, -> thousands
        b = _compute_brackets(claims, [ub for ub, _ in BASE_CLAIMS])
        if b:
            BASE_CLAIMS = b
        cpi_idx = await fred_series("CPIAUCSL", 61)                      # ~5y monthly index
        cpi_mm = [100.0 * (cpi_idx[i] / cpi_idx[i - 1] - 1.0)
                  for i in range(1, len(cpi_idx))]
        b2 = _compute_brackets(cpi_mm, [ub for ub, _ in BASE_CPI])
        if b2:
            BASE_CPI = b2
        # PATCH 14: keep the RAW samples too - bracket-mass tables cannot
        # answer threshold questions ("above X%?"), the distribution can.
        cpi_yoy = [100.0 * (cpi_idx[i] / cpi_idx[i - 12] - 1.0)
                   for i in range(12, len(cpi_idx)) if cpi_idx[i - 12] > 0]
        ECON_DIST["claims"] = claims
        ECON_DIST["cpi_mm"] = cpi_mm
        ECON_DIST["cpi_yoy"] = cpi_yoy
        ECON_STATE["fred"] = f"claims n={len(claims)}, cpi n={len(cpi_mm)}, yoy n={len(cpi_yoy)}"
        log.info(f"econ base rates loaded from FRED ({ECON_STATE['fred']})")
    except Exception as e:
        log.error(f"FRED refresh failed, keeping static tables: {e}")
        ECON_STATE["fred"] = f"error: {e}"

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

async def kalshi_delete(endpoint: str):
    headers = _sign_headers("DELETE", endpoint)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15) as c:
        r = await c.delete(endpoint, headers=headers)
        return r.json() if r.text else {}

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
    """Return (spot, sigma_per_minute, dampened_drift_per_minute, r15, r45).
    r15 = trailing 15m log return (recalibrate + momentum); r45 = trailing
    45m log return (PATCH 7b session trend - catches slow grind-downs that
    never trip the 15m threshold)."""
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
    r45 = math.log(closes[-1] / closes[-45]) if len(closes) >= 46 else r15
    return closes[-1], sigma, drift, r15, r45

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
    "guard_blacklist": [],   # PATCH 7b: tickers dump-guarded off; no re-entry til next day
    "maker_orders": [],      # PATCH 10: resting bids awaiting fill/TTL
    "press": {},             # PATCH 13: series -> current pressed size
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
        STATE["guard_blacklist"] = []
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
    sz = size_for(series)  # PATCH 12: per-series contract size

    f = result["filters"]

    # Filter 0 — trading-hours whitelist (PATCH 8, Aug 10): the full-week
    # ledger (n=363) shows the edge only exists in certain UTC hours
    # (1,2,5,6,9,12,13,21,23 profitable; e.g. hour 22 = PF 0.26).
    hr_now = datetime.now(timezone.utc).hour
    f["hours"] = hr_now in TRADING_HOURS_UTC
    result["hour_utc"] = hr_now
    if not f["hours"]:
        result["reason"] = f"outside trading hours (utc hr {hr_now}, whitelist {sorted(TRADING_HOURS_UTC)})"
        return result

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
        spot, sigma, drift, r15, r45 = await binance_stats(symbol)
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

    # ---------------------------------------------------- PATCH 7: momentum --
    mid = (yes_bid + yes_ask) / 2.0
    sigma_15 = sigma * math.sqrt(15.0)
    mom_thr = max(MOM_K * sigma_15, MOM_FLOOR_PCT)
    mom_down = r15 <= -mom_thr
    mom_up = r15 >= mom_thr
    momentum = "down" if mom_down else ("up" if mom_up else "flat")

    # 7b-i) session trend: a slow grind is invisible at 15m scale but obvious
    # over MOM_SESSION_MINUTES. This is what bled the account overnight Aug 9.
    sigma_sess = sigma * math.sqrt(MOM_SESSION_MINUTES)
    sess_thr = max(MOM_SESSION_K * sigma_sess, MOM_FLOOR_PCT)
    sess_down = r45 <= -sess_thr
    sess_up = r45 >= sess_thr
    session = "down" if sess_down else ("up" if sess_up else "flat")

    # 7a) alignment: refuse to trade INTO momentum. The 15m rule guards the
    # late window; the session rule guards the WHOLE window against grinds.
    yes_align_ok = not ((mom_down and mins < MOM_ALIGN_MINUTES) or sess_down)
    no_align_ok = not ((mom_up and mins < MOM_ALIGN_MINUTES) or sess_up)

    # 7b-ii) override is MOMENTUM-LED (was model-led, unreachable): the tape
    # sets direction; the model only has to NOT disagree (p < 0.55 for NO,
    # > 0.45 for YES). Inside the neutral band the stricter edge still applies.
    no_override = (mom_down or sess_down) and p < MOM_OVERRIDE_PMAX and mins >= MOM_OVERRIDE_MIN_MINUTES
    yes_override = (mom_up or sess_up) and p > (1.0 - MOM_OVERRIDE_PMAX) and mins >= MOM_OVERRIDE_MIN_MINUTES

    mid_yes_ok = mid >= (50.0 - MOM_OVERRIDE_BAND if yes_override else 50.0)
    mid_no_ok = mid < (50.0 + MOM_OVERRIDE_BAND if no_override else 50.0)

    # Filter 5 — edge vs market, WITH the Consensus Gate AND the EV floor.
    # Consensus Gate: never fight the market's directional lean
    # (disagreement trades went ~0-13, agreement trades 6-1).
    # PATCH 2 (EV floor): edge must clear EDGE_THRESHOLD + taker fee.
    edge_yes = p - yes_ask / 100.0
    edge_no = (yes_bid / 100.0) - p

    fee_yes_c = kalshi_taker_fee_cents(yes_ask, sz) / max(sz, 1)
    fee_no_c = kalshi_taker_fee_cents(100.0 - yes_bid, sz) / max(sz, 1)
    floor_yes = EDGE_THRESHOLD + fee_yes_c / 100.0
    floor_no = EDGE_THRESHOLD + fee_no_c / 100.0
    # inside the override's neutral band, demand the stricter edge
    if yes_override and mid < 50.0:
        floor_yes = max(floor_yes, MOM_OVERRIDE_EDGE)
    if no_override and mid >= 50.0:
        floor_no = max(floor_no, MOM_OVERRIDE_EDGE)

    if edge_yes >= edge_no and edge_yes >= floor_yes and mid_yes_ok and yes_align_ok:
        side, price_c, edge = "yes", yes_ask, edge_yes
    elif edge_no > edge_yes and edge_no >= floor_no and mid_no_ok and no_align_ok:
        side, price_c, edge = "no", 100.0 - yes_bid, edge_no
    else:
        side, price_c, edge = None, None, max(edge_yes, edge_no)
    f["edge"] = side is not None
    used_override = (side == "yes" and mid < 50.0) or (side == "no" and mid >= 50.0)
    result.update(edge=round(edge, 4), side=side, limit_price_cents=price_c,
                  ev_floor=round(min(floor_yes, floor_no), 4),
                  r15=round(r15, 5), r45=round(r45, 5), mid=round(mid, 1),
                  momentum=momentum, session=session, override=used_override)
    if not f["edge"]:
        would = "yes" if edge_yes >= edge_no else "no"
        raw_floor = floor_yes if would == "yes" else floor_no
        if would == "yes" and not yes_align_ok:
            if sess_down:
                result["reason"] = f"session trend down (r45 {round(r45*100,2)}%), no YES into a grind"
            else:
                result["reason"] = f"momentum block: tape falling (r15 {round(r15*100,2)}%), no YES late in window"
        elif would == "no" and not no_align_ok:
            if sess_up:
                result["reason"] = f"session trend up (r45 {round(r45*100,2)}%), no NO into a grind"
            else:
                result["reason"] = f"momentum block: tape rising (r15 {round(r15*100,2)}%), no NO late in window"
        elif (would == "yes" and not mid_yes_ok) or (would == "no" and not mid_no_ok):
            result["reason"] = f"edge {round(edge, 3)} but against market lean and no momentum override"
        elif max(edge_yes, edge_no) >= EDGE_THRESHOLD:
            result["reason"] = f"edge {round(edge, 3)} dies to fees (EV floor {round(raw_floor, 3)})"
        else:
            result["reason"] = f"edge {round(edge, 3)} below threshold {EDGE_THRESHOLD}"
        return result

    # Filter 6 — risk limits (trade count, duplicates, cooldown, daily spend cap)
    cooled = STATE["attempt_cooldown"].get(ticker, 0)
    cost_cents = (price_c or 0) * sz
    spend_cap_cents = DAILY_LOSS_LIMIT * 100.0
    over_spend_cap = spend_cap_cents > 0 and (STATE["spent_today_cents"] + cost_cents) > spend_cap_cents
    f["risk"] = (
        STATE["trades_today"] < MAX_TRADES_PER_DAY
        and ticker not in STATE["traded_tickers"]
        and ticker not in STATE["guard_blacklist"]
        and all(mo["ticker"] != ticker for mo in STATE["maker_orders"])
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
                f" (mid {'?' if fresh_mid is None else round(fresh_mid, 1)}c,"
                f" price {'?' if fresh_px is None else round(fresh_px, 1)}c,"
                f" band {MIN_PRICE_CENTS}-{MAX_PRICE_CENTS}c) - no order sent")
            return result
        price_c = fresh_px  # re-price off the LIVE book, not the stale snapshot
        if MAKER_MODE:
            order, mk_px = await post_maker_order(ticker, side, sz)
            if order and not order.get("ok") and "post only cross" in str(order.get("error", "")):
                # 10a: the book moved in the ~200ms between quote and post.
                # Re-read and retry ONCE at the fresh touch.
                order, mk_px = await post_maker_order(ticker, side, sz)
            result["order"] = order
            if order and order.get("ok"):
                oid = (order.get("response") or {}).get("order", {}).get("order_id") or \
                      (order.get("response") or {}).get("order_id")
                if oid:
                    STATE["maker_orders"].append({
                        "order_id": oid, "ticker": ticker, "side": side,
                        "px": mk_px, "count": float(sz), "ts": time.time(),
                        "p": p, "edge": edge, "mins": mins})
                    await tg_send(
                        f"MAKER POSTED\n{ticker}\nresting buy {side.upper()} x{sz} @ {round(mk_px, 1)}c"
                        f" (0 fee if filled, ttl {int(MAKER_TTL_SEC)}s)\n"
                        f"model {round(p, 3)} - edge {round(edge, 3)} - expires in {round(mins, 1)}m\n"
                        f"r15 {round(r15 * 100, 2)}% - mid {round(mid, 1)}c - momentum {momentum} - trend {session}")
                else:
                    await tg_send(f"MAKER POST FAILED\n{ticker}\nno order id returned - not counted, no cost")
            else:
                why = (order or {}).get("error") or f"maker price {mk_px} outside band {MIN_PRICE_CENTS}-{MAX_PRICE_CENTS}c"
                await tg_send(f"MAKER POST FAILED\n{ticker}\n{str(why)[:200]}")
            return result
        order = await place_order(ticker, side, price_c, sz)
        result["order"] = order
        if order.get("ok"):
            filled_n = float(order.get("filled") or 0)
            if filled_n > 0:
                # Only real fills consume the daily trade/spend caps.
                STATE["trades_today"] += 1
                STATE["spent_today_cents"] += (price_c + TAKER_BUFFER_CENTS) * min(filled_n, float(sz))
                STATE["traded_tickers"].append(ticker)
                pos = {"ticker": ticker, "side": side,
                       "count": min(filled_n, float(sz)),
                       "entry_c": price_c + TAKER_BUFFER_CENTS, "ts": time.time()}
                STATE["open_positions"].append(pos)
                tag = "FILLED" if filled_n >= sz else f"PARTIAL {filled_n:g}/{sz}"
            else:
                tag = "NOT FILLED (book moved - no cost, not counted)"
            ov_tag = ""
            if side == "no" and no_override and mid >= 50.0:
                ov_tag = " [MOM OVERRIDE]"
            elif side == "yes" and yes_override and mid < 50.0:
                ov_tag = " [MOM OVERRIDE]"
            await tg_send(
                f"ORDER {tag}{ov_tag}\n{ticker}\nbuy {side.upper()} x{sz} @ {round(price_c, 1)}c\n"
                f"model {round(p, 3)} - edge {round(edge, 3)} - expires in {round(mins, 1)}m\n"
                f"r15 {round(r15 * 100, 2)}% - mid {round(mid, 1)}c - momentum {momentum} - trend {session}"
            )
            if filled_n > 0:
                # GUARD 2 - crash-fill dump (PATCH 3: hardened lookup).
                fill_px = await actual_fill_price_cents(ticker, side, order)
                if fill_px is not None:
                    log.info(f"fill price {ticker}: {round(fill_px, 1)}c (floor {FILL_FLOOR_CENTS:g}c)")
                if fill_px is not None and fill_px < FILL_FLOOR_CENTS:
                    if await close_position(
                        pos, f"crash-fill guard: filled {round(fill_px, 1)}c < {FILL_FLOOR_CENTS:g}c"):
                        STATE["open_positions"].remove(pos)
                        # PATCH 7b: never re-buy the knife we just dumped.
                        if ticker not in STATE["guard_blacklist"]:
                            STATE["guard_blacklist"].append(ticker)
                            await tg_send(f"GUARD BLACKLIST\n{ticker}\nno re-entry on this contract.")
                    else:
                        # PATCH 7d: a failed dump used to be silent (the 54c
                        # fill that held to settlement, Aug 9). Now it yells.
                        await tg_send(
                            f"GUARD DUMP FAILED\n{ticker}\nfilled {round(fill_px, 1)}c"
                            f" < {FILL_FLOOR_CENTS:g}c floor but the exit order did NOT fill."
                            f" SELL IT IN THE APP NOW.")
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
        # PATCH 7d: re-check the PRICE BAND too, not just consensus. The 74c
        # fill (Aug 9) and the 54c slide both passed the old mid-only check
        # after the quote moved between scan and order.
        if side == "yes":
            ok = mid >= 50.0 and MIN_PRICE_CENTS <= ask <= MAX_PRICE_CENTS
            return ok, ask, mid                  # re-priced at the LIVE ask
        no_px = 100.0 - bid
        ok = mid < 50.0 and MIN_PRICE_CENTS <= no_px <= MAX_PRICE_CENTS
        return ok, no_px, mid                    # NO priced off the LIVE bid
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

async def post_maker_order(ticker: str, side: str, count: int):
    """PATCH 10: post a resting bid at the live quote. YES rests at yes_bid;
    NO rests at (100 - yes_ask) = the NO bid. Band-checked on the maker px."""
    data = await kalshi_get(f"/markets/{ticker}")
    m = data.get("market", {})
    if side == "yes":
        px = cents(m, "yes_bid")
    else:
        ask = cents(m, "yes_ask")
        px = None if ask is None else 100.0 - ask
    if px is None or not (MIN_PRICE_CENTS <= px <= MAX_PRICE_CENTS):
        return None, px
    order = await place_order(ticker, side, px, count, maker=True)
    return order, px

async def manage_maker_orders():
    """PATCH 10: poll resting maker orders each scan. Fills consume the daily
    caps exactly like taker fills (0 maker fee); stale orders are canceled."""
    pending = STATE["maker_orders"]
    if not pending:
        return
    for mo in list(pending):
        try:
            d = await kalshi_get(f"/portfolio/orders/{mo['order_id']}")
            o = d.get("order", d) if isinstance(d, dict) else {}
            raw = o.get("fill_count_fp") or o.get("fill_count") or "0"
            try:
                filled = float(raw)
            except (TypeError, ValueError):
                filled = 0.0
            status = str(o.get("status") or "")
            age = time.time() - mo["ts"]
            if filled > 0:
                n = min(filled, mo["count"])
                STATE["trades_today"] += 1
                STATE["spent_today_cents"] += mo["px"] * n
                STATE["traded_tickers"].append(mo["ticker"])
                pos = {"ticker": mo["ticker"], "side": mo["side"], "count": n,
                       "entry_c": mo["px"], "ts": time.time()}
                STATE["open_positions"].append(pos)
                pending.remove(mo)
                await tg_send(
                    f"MAKER FILLED\n{mo['ticker']}\nbuy {mo['side'].upper()} x{filled:g} @ {round(mo['px'], 1)}c"
                    f" (0 maker fee, rested {int(age)}s)\n"
                    f"model {round(mo['p'], 3)} - edge {round(mo['edge'], 3)} - expires in {round(mo['mins'], 1)}m")
                # crash-fill guard applies to maker fills too
                if mo["px"] < FILL_FLOOR_CENTS:
                    if await close_position(pos, f"crash-fill guard: maker fill {round(mo['px'], 1)}c < {FILL_FLOOR_CENTS:g}c"):
                        STATE["open_positions"].remove(pos)
                        if mo["ticker"] not in STATE["guard_blacklist"]:
                            STATE["guard_blacklist"].append(mo["ticker"])
                            await tg_send(f"GUARD BLACKLIST\n{mo['ticker']}\nno re-entry on this contract.")
            elif status in ("canceled", "cancelled", "failed", "expired") or age > MAKER_TTL_SEC:
                try:
                    await kalshi_delete(f"/portfolio/orders/{mo['order_id']}")
                except Exception:
                    pass
                pending.remove(mo)
                log.info(f"maker order gone unfilled: {mo['ticker']} ({status or 'ttl'}, {int(age)}s)")
                await tg_send(
                    f"MAKER EXPIRED\n{mo['ticker']}\nrested {int(age)}s unfilled - canceled, no cost")
        except Exception as e:
            log.warning(f"maker manage error {mo.get('ticker')}: {e}")

async def update_press_state():
    """PATCH 13: settle-watcher for press series. Each scan, check open
    press-series positions; when the market has settled, press the next
    trade's size up on a win, reset to base on a loss, and prune the
    settled position (also fixes stale-position buildup for these series)."""
    if not PRESS_SERIES:
        return
    for pos in list(STATE["open_positions"]):
        series = next((s for s in PRESS_SERIES if pos["ticker"].startswith(s)), None)
        if not series:
            continue
        try:
            data = await kalshi_get(f"/markets/{pos['ticker']}")
            m = data.get("market", {})
            result = (m.get("result") or "").lower()
            if result not in ("yes", "no"):
                continue  # still trading
            win = pos["side"] == result
            base = SERIES_SIZE.get(series, 1)
            cur = STATE["press"].get(series, base)
            new = min(cur + 1, PRESS_MAX) if win else base
            STATE["press"][series] = new
            STATE["open_positions"].remove(pos)
            n = pos.get("count", base)
            pnl = (100.0 - pos["entry_c"]) * n / 100.0 if win else -pos["entry_c"] * n / 100.0
            await tg_send(
                f"PRESS {'UP' if win and new > base else ('SET' if win else 'RESET')}\n{pos['ticker']}\n"
                f"{'WIN' if win else 'LOSS'} {'+' if pnl >= 0 else ''}{round(pnl, 2)}$ "
                f"-> next {series} size {new}")
        except Exception as e:
            log.warning(f"press update error {pos.get('ticker')}: {e}")

async def place_order(ticker: str, side: str, price_cents: float, count: int, reduce_only: bool = False, maker: bool = False):
    """Kalshi Create Order V2: /portfolio/events/orders.
    - buy YES at p cents -> side="bid", price=p/100
    - buy NO at q cents -> side="ask", price=(100-q)/100
    """
    if maker:
        # Resting order at the exact quote: no buffer, post_only, GTC.
        v2_price = min(max(price_cents, 1.0), 99.0) / 100.0
        v2_side = "bid" if side == "yes" else "ask"
    elif side == "yes":
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
        "time_in_force": "good_till_canceled" if maker else "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": maker,
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

# ------------------------------------------------------- PATCH 16: journal --
# The tape recorder. Every scan, one NDJSON line per series: the full signal
# context INCLUDING rejections, so losses can be studied against the minutes
# before entry. Read-only - journaling must never break a scan.
JOURNAL_FILE = "/tmp/scan_journal.ndjson"

def _journal_scan(results):
    try:
        ts = datetime.now(timezone.utc).isoformat()
        with open(JOURNAL_FILE, "a") as fh:
            for r in results:
                rec = {"ts": ts, "series": r.get("series"), "side": r.get("side"),
                       "edge": r.get("edge"), "model_p": r.get("model_prob"),
                       "yes_bid": r.get("yes_bid"), "yes_ask": r.get("yes_ask"),
                       "mid": r.get("mid"), "r15": r.get("r15"), "r45": r.get("r45"),
                       "momentum": r.get("momentum"), "session": r.get("session"),
                       "mins": r.get("minutes_to_expiry"), "hour_utc": r.get("hour_utc"),
                       "spot": r.get("spot"), "strike": r.get("strike"),
                       "ticker": r.get("ticker"), "proceed": r.get("proceed"),
                       "override": r.get("override"), "reason": r.get("reason")}
                fh.write(json.dumps(rec) + "\n")
        # cheap rotation: keep the file under ~12MB so a long week can't bloat
        if os.path.getsize(JOURNAL_FILE) > 12_000_000:
            with open(JOURNAL_FILE) as fh:
                lines = fh.readlines()
            with open(JOURNAL_FILE, "w") as fh:
                fh.writelines(lines[-len(lines) // 2:])
            log.info("journal rotated (kept newest half)")
    except Exception as e:
        log.warning(f"journal write failed: {e}")

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
    _journal_scan(out)  # PATCH 16: record the tape (rejections included)
    return out


async def rehydrate_state():
    """PATCH 7c: rebuild today's counters from the ledger at boot.
    Before this, every redeploy reset trades_today / spent_today_cents /
    traded_tickers to zero - the 25-trade daily cap and spend cap silently
    doubled on patch days (observed Aug 9: 25/25 filled, redeploy, fresh 25)."""
    try:
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        data = await kalshi_get("/portfolio/fills", params={"limit": 200})
        trades, spent, tickers = 0, 0.0, []
        for f in data.get("fills", []):
            ts = f.get("created_time") or f.get("ts")
            try:
                fdt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                continue
            if fdt < day_start:
                continue                      # only today's ledger
            if (f.get("action") or "").lower() != "buy":
                continue                      # entries only; exits don't consume caps
            side = (f.get("side") or "yes").lower()
            px = f.get("yes_price") if side == "yes" else f.get("no_price")
            if px is None:
                px = f.get("price")
            if px is None:
                # PATCH 7d: newer API returns dollar-encoded fields; the
                # $0.0-spent bug (Aug 9) was reading cents fields that
                # arrived as None.
                raw = (f.get("yes_price_dollars") if side == "yes"
                       else f.get("no_price_dollars")) or f.get("price_dollars")
                px = float(raw) * 100.0 if raw is not None else None
            if px is None:
                continue
            px = float(px)
            if px <= 1.0:
                px *= 100.0          # tolerate dollar encoding in any field
            # PATCH 7e: new API returns count_fp (fixed-point string); the
            # old "count" key arrives absent -> cnt 0 -> $0.0 spent bug.
            cnt = float(f.get("count") or f.get("count_fp") or 0)
            trades += 1
            spent += px * cnt
            tk = f.get("ticker")
            if tk and tk not in tickers:
                tickers.append(tk)
        STATE["trades_today"] = trades
        STATE["spent_today_cents"] = spent
        STATE["traded_tickers"] = tickers
        # PATCH 7e: stamp the day, else the first reset_daily() call after
        # boot sees day="" and zeroes everything we just rebuilt (observed
        # Aug 9: REHYDRATED 45/25 -> traded again 2 minutes later).
        STATE["day"] = day_start.date().isoformat()
        log.info(f"rehydrated: {trades} trades, {round(spent,1)}c spent, {len(tickers)} tickers today")
        await tg_send(
            f"STATE REHYDRATED\ntoday so far: {trades}/{MAX_TRADES_PER_DAY} trades, "
            f"${round(spent/100.0, 2)} spent.\nCaps survive restarts now.")
    except Exception as e:
        log.error(f"rehydrate failed: {e}")
        STATE["last_error"] = f"rehydrate: {e}"
        await tg_send(
            f"REHYDRATE FAILED\n{e}\nDaily counters are zeroed - the bot has a "
            f"fresh {MAX_TRADES_PER_DAY}-trade budget it should not have. Watch it.")

async def auto_loop():
    await asyncio.sleep(10)
    await rehydrate_state()
    log.info(f"scanner up: {SCAN_SERIES} every {SCAN_INTERVAL_SEC}s - auto_trade={AUTO_TRADE}")
    await tg_send(f"SixFilter online.\nScanning: {', '.join(SCAN_SERIES)}\nAuto-trade: {AUTO_TRADE}")
    if MAKER_MODE:
        try:
            d = await kalshi_get("/portfolio/orders")
            n_cancel = 0
            for o in (d.get("orders", []) if isinstance(d, dict) else []):
                oid = o.get("order_id")
                if oid:
                    await kalshi_delete(f"/portfolio/orders/{oid}")
                    n_cancel += 1
            log.info(f"maker startup sweep: canceled {n_cancel} stray resting order(s)")
        except Exception as e:
            log.warning(f"maker startup sweep failed: {e}")
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
        try:
            await manage_maker_orders()
        except Exception as e:
            log.error(f"maker manager error: {e}")
        try:
            await update_press_state()
        except Exception as e:
            log.error(f"press update error: {e}")
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
    "crypto_pm": [],      # PATCH 15: Poly crypto up/down inventory
    "cand_gaps": [],      # PATCH 15: unverified cross-venue gap candidates
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

    # --- PATCH 15: Poly crypto up/down inventory + candidate gap logger ---
    # User's Polymarket access is solved; before any build we MEASURE: are
    # Poly's crypto binaries priced softer than Kalshi's? Candidate gaps are
    # auto-matched by theme for measurement ONLY - resolution terms are NOT
    # verified, so these never route to orders without a manual check.
    THEMES = {"btc": ("bitcoin", "KXBTC15M"), "eth": ("ethereum", "KXETH15M"),
              "sol": ("solana", None), "xrp": ("xrp", None)}
    crypto_rows = []
    for m in markets:
        q = (m.get("question") or "") + " " + (m.get("slug") or "")
        ql = q.lower()
        if not any(t[0] in ql for t in THEMES.values()):
            continue
        if not ("up or down" in ql or "15m" in ql or "15-min" in ql
                or "hourly" in ql or "1h" in ql or "updown" in ql):
            continue
        yes = _poly_yes_price(m)
        theme = next(k for k, t in THEMES.items() if t[0] in ql)
        crypto_rows.append({
            "theme": theme, "question": (m.get("question") or "")[:100],
            "slug": (m.get("slug") or "")[:80],
            "yes_c": round(yes, 1) if yes is not None else None,
            "vol24h": round(float(m.get("volume24hr") or m.get("volume") or 0)),
            "liquidity": round(float(m.get("liquidity") or 0)),
            "ends": str(m.get("endDate") or m.get("end_date") or "")[:16],
        })
    POLY_STATE["crypto_pm"] = crypto_rows[:25]

    # candidate gaps vs Kalshi 15M mids (BTC/ETH only - SOL/XRP unmatched yet)
    for theme, (kw, kseries) in THEMES.items():
        if not kseries:
            continue
        best = next((r for r in crypto_rows
                     if r["theme"] == theme and r["yes_c"] is not None), None)
        if not best:
            continue
        try:
            kd = await kalshi_get("/markets", params={
                "series_ticker": kseries, "status": "open", "limit": 5})
            km = None
            for cand in kd.get("markets", []):
                b, a = cents(cand, "yes_bid"), cents(cand, "yes_ask")
                if b is not None and a is not None:
                    km = (cand.get("ticker"), (a + b) / 2.0)
                    break
            if not km:
                continue
            gap = best["yes_c"] - km[1]
            row = {"ts": datetime.now(timezone.utc).strftime("%m-%d %H:%M"),
                   "theme": theme, "poly_c": best["yes_c"], "kalshi_c": round(km[1], 1),
                   "gap_c": round(gap, 1), "kalshi_ticker": km[0],
                   "poly_slug": best["slug"][:60]}
            POLY_STATE["cand_gaps"].insert(0, row)
            last = POLY_STATE["gap_alerted_at"].get(f"cand:{theme}", 0)
            if abs(gap) >= POLY_GAP_ALERT_C and time.time() - last > 3600:
                POLY_STATE["gap_alerted_at"][f"cand:{theme}"] = time.time()
                await tg_send(
                    f"POLY-KALSHI CANDIDATE GAP {abs(gap):.1f}c ({theme.upper()})\n"
                    f"Poly {best['yes_c']}c / Kalshi {km[1]:.1f}c ({km[0]})\n"
                    f"{best['question'][:80]}\n"
                    f"UNVERIFIED TERMS - measurement only, no money")
        except Exception as e:
            log.warning(f"cand gap {theme}: {e}")
    POLY_STATE["cand_gaps"] = POLY_STATE["cand_gaps"][:50]
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

# --------------------------------------------- econ base-rate engine --------
ECON_STATE = {
    "last_scan": None, "last_error": None,
    "series_found": [], "markets": [],          # latest inventory snapshot
    "signals": [], "settled": [],               # paper ledger
    "seen": set(), "alerted": {},
    "unparsed": 0, "fred": None,
    "fred_last_refresh": 0.0,
}

def _bracket_prob(table, x):
    """Probability that the value falls in the bracket containing x."""
    prev = 0.0
    for ub, p in table:
        if x < ub:
            return p
        prev = ub
    return table[-1][1]

def econ_base_rate(series_title: str, m: dict):
    """Return (base_prob_yes, label) or (None, reason). Defensive v1 parsing -
    anything unrecognized is logged as unparsed so we fix regexes off REAL
    market titles rather than guessing."""
    t = " ".join(str(m.get(k) or "") for k in
                 ("title", "subtitle", "yes_sub_title", "no_sub_title")).lower()
    st = (series_title or "").lower()
    try:
        if any(k in st for k in ("fed", "fomc", "interest rate")):
            if any(k in t for k in ("unchanged", "no change", "keep", "maintain", "hold")):
                return BASE_FOMC["hold"], "fomc:hold"
            if any(k in t for k in ("cut", "decrease", "lower")):
                return (BASE_FOMC["cut50"] if "50" in t else BASE_FOMC["cut25"]), "fomc:cut"
            if any(k in t for k in ("raise", "hike", "increase")):
                return (BASE_FOMC["hike50"] if "50" in t else BASE_FOMC["hike25"]), "fomc:hike"
            return None, "fomc:unparsed"
        if any(k in st for k in ("claim", "jobless", "unemployment insurance")):
            # threshold form: "above 250,000" / "below 250K"
            mnum = re.search(r"(\d{3}),?(\d{3})", t)
            mk = re.search(r"(\d{3})\s?k", t)
            x = (int(mnum.group(1) + mnum.group(2)) / 1000.0) if mnum else \
                (float(mk.group(1)) if mk else None)
            if x is None:
                return None, "claims:unparsed"
            below = any(k in t for k in ("below", "under", "less than", "or less"))
            p = dist_cdf("claims", x) if below else 1.0 - dist_cdf("claims", x)
            return min(max(p, 0.01), 0.99), f"claims:{'<' if below else '>'}{x:.0f}K"
        if "cpi" in st or "inflation" in st:
            nums = re.findall(r"(-?\d+\.\d)\s?%", t)
            if not nums:
                return None, "cpi:unparsed"
            x = float(nums[0])
            # YoY questions ("12-month change", "year end", *CPIYEAR*) must be
            # measured on the YoY distribution - v1 used m/m (unit mismatch).
            yoy = ("12-month" in t or "year" in st or "yoy" in t or "annual" in t)
            kind = "cpi_yoy" if yoy else "cpi_mm"
            below = any(k in t for k in ("below", "under", "less than", "or less"))
            p = dist_cdf(kind, x) if below else 1.0 - dist_cdf(kind, x)
            return min(max(p, 0.01), 0.99), f"{kind}:{'<' if below else '>'}{x}%"
        return None, "family:unknown"
    except Exception:
        return None, "parse:error"

ECON_PAPER_FILE = "/tmp/econ_paper.ndjson"

def _econ_log(rec: dict):
    """PATCH 14: append-only paper ledger. Best-effort; never crash a scan."""
    try:
        with open(ECON_PAPER_FILE, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception as e:
        log.warning(f"econ paper log: {e}")

def _econ_rehydrate():
    """Rebuild the paper ledger after a redeploy so the W/L record and the
    seen-set (no duplicate alerts) survive. Called once at scanner boot."""
    try:
        if not os.path.exists(ECON_PAPER_FILE):
            return
        sigs, settled = {}, {}
        for line in open(ECON_PAPER_FILE):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") == "signal":
                sigs[r["ticker"]] = r["sig"]
            elif r.get("type") == "result":
                settled[r["ticker"]] = r
        for tick, sig in sigs.items():
            ECON_STATE["seen"].add(tick)
            if tick in settled:
                res = settled[tick]
                sig.update(settled=True, result=res.get("result"),
                           won=res.get("won"), pnl_c=res.get("pnl_c"))
                ECON_STATE["settled"].append(sig)
            else:
                ECON_STATE["signals"].append(sig)
        log.info(f"econ paper rehydrated: {len(settled)} settled, "
                 f"{len(ECON_STATE['signals'])} open")
    except Exception as e:
        log.warning(f"econ rehydrate failed (fresh ledger): {e}")

async def econ_scan_once():
    # 1) discover econ series (cached per process; refreshed each restart)
    if not ECON_STATE["series_found"]:
        found, cursor = [], ""
        for _ in range(5):
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await kalshi_get("/series", params=params)
            for sr in data.get("series", []):
                title = f"{sr.get('ticker','')} {sr.get('title','')}".lower()
                if any(k in title for k in ECON_KEYWORDS):
                    found.append({"ticker": sr.get("ticker"), "title": sr.get("title")})
            cursor = data.get("cursor") or ""
            if not cursor:
                break
        ECON_STATE["series_found"] = found[:ECON_MAX_SERIES]
        log.info(f"econ series discovered: {[s['ticker'] for s in ECON_STATE['series_found']]}")

    # 2) scan open markets in those series
    markets_out = []
    for sr in ECON_STATE["series_found"]:
        try:
            data = await kalshi_get("/markets", params={
                "series_ticker": sr["ticker"], "status": "open", "limit": 40})
        except Exception as e:
            log.warning(f"econ markets {sr['ticker']}: {e}")
            continue
        for m in data.get("markets", []):
            bid, ask = cents(m, "yes_bid"), cents(m, "yes_ask")
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2.0
            base, label = econ_base_rate(sr.get("title", ""), m)
            if base is None:
                ECON_STATE["unparsed"] += 1
            row = {"ticker": m.get("ticker"), "title": (m.get("title") or "")[:100],
                   "mid_c": round(mid, 1), "base": round(base * 100, 1) if base else None,
                   "label": label, "expires": str(m.get("expiration_time") or "")[:10]}
            markets_out.append(row)

            # 3) divergence -> paper signal + alert
            if base is None:
                continue
            gap = base - mid / 100.0
            tick = m.get("ticker")
            if abs(gap) >= ECON_GAP_ALERT and tick not in ECON_STATE["seen"]:
                ECON_STATE["seen"].add(tick)
                side = "YES" if gap > 0 else "NO"
                price_c = mid if side == "YES" else 100.0 - mid
                sig = {"ticker": tick, "title": row["title"], "side": side,
                       "entry_c": round(price_c, 1), "base_pct": round(base * 100, 1),
                       "gap_pts": round(abs(gap) * 100, 1),
                       "ts": datetime.now(timezone.utc).strftime("%m-%d %H:%M"),
                       "settled": False}
                ECON_STATE["signals"].insert(0, sig)
                _econ_log({"type": "signal", "ticker": tick, "sig": sig})
                await tg_send(
                    f"ECON PAPER SIGNAL (no money)\n{row['title'][:80]}\n"
                    f"base rate {base*100:.0f}% vs market {mid:.1f}c -> gap {abs(gap)*100:.0f} pts\n"
                    f"paper BUY {side} @ {price_c:.1f}c | grading at settlement")
    ECON_STATE["markets"] = markets_out[:60]

    # 4) grade paper signals whose markets have settled
    for sig in ECON_STATE["signals"]:
        if sig["settled"]:
            continue
        try:
            data = await kalshi_get(f"/markets/{sig['ticker']}")
            m = data.get("market", {})
            result = str(m.get("result") or "").lower()
            if result not in ("yes", "no"):
                continue
            won = ((sig["side"] == "YES") == (result == "yes"))
            pnl_c = (100.0 - sig["entry_c"]) if won else -sig["entry_c"]
            sig["settled"] = True
            sig["result"] = result; sig["won"] = won; sig["pnl_c"] = round(pnl_c, 1)
            ECON_STATE["settled"].insert(0, dict(sig))
            _econ_log({"type": "result", "ticker": sig["ticker"],
                       "result": result, "won": won, "pnl_c": round(pnl_c, 1)})
            n = len(ECON_STATE["settled"])
            w = sum(1 for x in ECON_STATE["settled"] if x["won"])
            tot = sum(x["pnl_c"] for x in ECON_STATE["settled"])
            await tg_send(
                f"ECON PAPER RESULT: {'WIN' if won else 'LOSS'} ({pnl_c:+.0f}c)\n"
                f"{sig['title'][:70]}\n"
                f"paper record: {w}W-{n-w}L, {tot:+.0f}c total")
        except Exception as e:
            log.warning(f"econ grade {sig['ticker']}: {e}")
    ECON_STATE["signals"] = ECON_STATE["signals"][:100]  # PATCH 14: bound memory
    ECON_STATE["last_scan"] = datetime.now(timezone.utc).isoformat()

async def econ_loop():
    await asyncio.sleep(25)
    _econ_rehydrate()
    if not ECON_ENABLED:
        log.info("econ scanner disabled (ECON_ENABLED=false)")
        return
    try:
        await kalshi_get("/exchange/status")
        await refresh_base_rates()
        src = f"base rates: FRED live ({ECON_STATE.get('fred')})" if ECON_STATE.get("fred", "").startswith("claims") \
              else "base rates: static tables (no FRED key)"
        log.info("econ base-rate scanner online (paper-only)")
        await tg_send("Econ base-rate scanner online (PAPER ONLY - no orders, no money).\n"
                      "Watching FOMC/CPI/claims markets for retail-vs-base-rate gaps.\n"
                      f"{src}\nBoard: /econ/board")
    except Exception as e:
        ECON_STATE["last_error"] = str(e)
        log.error(f"econ scanner probe failed: {e}")
        return
    while True:
        try:
            if time.time() - ECON_STATE["fred_last_refresh"] > 86400:
                ECON_STATE["fred_last_refresh"] = time.time()
                await refresh_base_rates()
            await econ_scan_once()
        except Exception as e:
            ECON_STATE["last_error"] = str(e)
            log.error(f"econ scan error: {e}")
        await asyncio.sleep(ECON_SCAN_SEC)

# -------------------------------------------------------------------- app ---
app = FastAPI(title="SixFilter Kalshi Trader API", docs_url="/docs")

@app.on_event("startup")
async def _startup():
    load_key()
    asyncio.create_task(auto_loop())
    asyncio.create_task(poly_loop())
    asyncio.create_task(econ_loop())

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

# ============================================================
# POST /backtest/pnl — Trading-Factory compatibility
# ============================================================
@app.post("/backtest/pnl")
async def backtest_pnl_factory(request: Request):
    """
    Trading-Factory lab calls this with:
      {"params": {"symbol":"BTCUSDT","days":7,"band_low":58,"band_high":72,"edge":0.08,"fee":1}}
    Returns:
      {"pnls": [0.125, -0.083, ...], "n_trades": 100, "profit_factor": 1.4}
    PnLs are in DOLLARS (not cents) to match factory expectations.
    """
    try:
        data = await request.json()
    except Exception:
        return {"pnls": [], "error": "invalid json"}

    params = data.get("params", {})
    symbol = params.get("symbol", "BTCUSDT")
    days = max(1, min(int(params.get("days", 7)), 14))
    min_price = float(params.get("band_low", params.get("min_price", 55.0)))
    max_price = float(params.get("band_high", params.get("max_price", 90.0)))
    edge = float(params.get("edge", 0.08))
    fee = int(params.get("fee", 1))

    closes = await _fetch_klines(symbol, days)
    if len(closes) < 500:
        return {"pnls": [], "error": "not enough kline data"}

    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    WINDOW = 15
    SPREAD_C = 2.0

    all_pnls = []
    for m_left in (13, 8, 4):
        t_eff = max(m_left - 0.5, 0.25)
        for d in range(125, len(closes) - m_left, 3):
            hist = rets[d - 120:d]
            sigma = statistics.pstdev(hist) if len(hist) > 2 else 0.001
            drift = statistics.mean(hist[-20:]) * 0.15
            spot = closes[d]
            strike = closes[d - (WINDOW - m_left)]
            win = closes[d + m_left] >= strike

            # FIX: raw model = synthetic market price (retail doesn't recalibrate)
            p_raw = prob_above(spot, strike, sigma, drift, t_eff)
            r15_bt = math.log(closes[d] / closes[d - WINDOW])
            # RECALIBRATED model = our edge
            p_model = recalibrate(p_raw, m_left, r15_bt, sigma, symbol)

            # Synthetic market centered at raw p
            yes_ask = p_raw * 100.0 + SPREAD_C
            yes_bid = p_raw * 100.0 - SPREAD_C
            mid = p_raw * 100.0

            if not (min_price <= yes_ask <= max_price):
                continue

            # Edge = recalibrated model vs raw market
            edge_yes = p_model - yes_ask / 100.0
            edge_no = (yes_bid / 100.0) - p_model

            if edge_yes >= edge_no and edge_yes >= edge and mid >= 50:
                side, entry_c = "yes", yes_ask
            elif edge_no > edge_yes and edge_no >= edge and mid < 50:
                side, entry_c = "no", 100.0 - yes_bid
            else:
                continue

            if abs(p_model - 0.5) < 0.02:
                continue

            fee_c = kalshi_taker_fee_cents(entry_c) if fee else 0.0
            if side == "yes":
                pnl = (100.0 - entry_c) if win else -entry_c
            else:
                pnl = (100.0 - entry_c) if not win else -entry_c
            pnl -= fee_c
            all_pnls.append(round(pnl / 100.0, 4))

    n = len(all_pnls)
    if n == 0:
        return {"pnls": [], "n_trades": 0, "profit_factor": 0.0, "win_rate": 0.0}

    wins = [x for x in all_pnls if x > 0]
    losses = [x for x in all_pnls if x < 0]
    pf = round(sum(wins) / max(0.01, abs(sum(losses))), 3) if losses else 999.0
    wr = round(len(wins) / n, 3)

    return {
        "pnls": all_pnls,
        "n_trades": n,
        "profit_factor": pf,
        "win_rate": wr,
        "total_pnl": round(sum(all_pnls), 2),
    }

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
        "series_sizes": {s: size_for(s) for s in SCAN_SERIES},
        "press": STATE["press"],
        "min_price_cents": MIN_PRICE_CENTS,
        "max_price_cents": MAX_PRICE_CENTS,
        "fill_floor_cents": FILL_FLOOR_CENTS,
        "mom": {"k": MOM_K, "align_minutes": MOM_ALIGN_MINUTES,
                "session_minutes": MOM_SESSION_MINUTES, "session_k": MOM_SESSION_K,
                "override_pmax": MOM_OVERRIDE_PMAX, "override_edge": MOM_OVERRIDE_EDGE,
                "floor_pct": MOM_FLOOR_PCT},
        "guard_blacklist": STATE["guard_blacklist"],
        "trading_hours_utc": sorted(TRADING_HOURS_UTC),
        "maker_mode": bool(MAKER_MODE),
        "maker_pending": len(STATE["maker_orders"]),
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
      (s.last_signals||[]).map(r=>`${r.series}: ${r.side||'-'} edge=${r.edge??'-'} ${r.reason||''}`).join('\\n') || 'no scans yet';
  }catch(e){ document.getElementById('s').textContent = 'dashboard error: '+e; }
}
load(); setInterval(load, 10000);
</script></body></html>"""

@app.get("/journal")
def journal_tail(n: int = 100, offset: int = -1):
    """PATCH 16/17: journal lines as JSON. Default (offset=-1) = last n lines
    (tail, phone-friendly). To page the WHOLE tape: offset=0, then follow
    next_offset until it comes back null."""
    if not os.path.exists(JOURNAL_FILE):
        return {"lines": [], "note": "no journal yet - starts on first scan after deploy"}
    n = max(1, min(int(n), 2000))
    try:
        with open(JOURNAL_FILE) as fh:
            all_lines = fh.readlines()
        total = len(all_lines)
        if offset is None or int(offset) < 0:
            lines = all_lines[-n:]
            nxt = None
        else:
            offset = max(0, int(offset))
            lines = all_lines[offset:offset + n]
            nxt = offset + len(lines) if offset + len(lines) < total else None
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return {"lines": out, "total_lines": total,
                "next_offset": nxt,
                "size_bytes": os.path.getsize(JOURNAL_FILE)}
    except Exception as e:
        return {"lines": [], "error": str(e)}

@app.get("/journal/download")
def journal_download(key: str = ""):
    """Full tape pull. Key-guarded like /export - download BEFORE redeploys.
    PATCH 17: read into memory and serve inline (FileResponse was stalling
    through the proxy on multi-MB files)."""
    if not _export_ok(key):
        return {"ok": False, "error": "disabled or bad key"}
    if not os.path.exists(JOURNAL_FILE):
        return {"ok": False, "error": "no journal yet"}
    from fastapi.responses import Response
    with open(JOURNAL_FILE, "rb") as fh:
        data = fh.read()
    return Response(content=data, media_type="application/x-ndjson",
                    headers={"Content-Disposition": "attachment; filename=scan_journal.ndjson"})

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
        "crypto_pm": POLY_STATE["crypto_pm"],      # PATCH 15
        "cand_gaps": POLY_STATE["cand_gaps"],      # PATCH 15
        "watched": POLY_STATE["watched"],
        "board": POLY_STATE["board"],
    }

@app.get("/poly/board", response_class=HTMLResponse)
def poly_board():
    """Polymarket intel page - open from your phone."""
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


@app.get("/econ")
def econ_status():
    """Econ base-rate paper scanner state (JSON)."""
    n = len(ECON_STATE["settled"])
    w = sum(1 for x in ECON_STATE["settled"] if x["won"])
    tot = sum(x["pnl_c"] for x in ECON_STATE["settled"])
    return {
        "enabled": ECON_ENABLED,
        "last_scan": ECON_STATE["last_scan"],
        "last_error": ECON_STATE["last_error"],
        "series_found": ECON_STATE["series_found"],
        "unparsed_count": ECON_STATE["unparsed"],
        "fred": ECON_STATE["fred"],
        "base_claims": BASE_CLAIMS, "base_cpi": BASE_CPI, "base_fomc": BASE_FOMC,
        "paper_record": {"settled": n, "wins": w, "win_rate": round(w / n, 3) if n else None,
                         "total_pnl_c": round(tot, 1)},
        "open_signals": [x for x in ECON_STATE["signals"] if not x["settled"]][:25],
        "settled": ECON_STATE["settled"][:25],
        "markets": ECON_STATE["markets"],
    }

@app.get("/econ/board", response_class=HTMLResponse)
def econ_board():
    """Mobile-friendly econ paper-trading page."""
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    n = len(ECON_STATE["settled"])
    w = sum(1 for x in ECON_STATE["settled"] if x["won"])
    tot = sum(x["pnl_c"] for x in ECON_STATE["settled"])
    open_sigs = [x for x in ECON_STATE["signals"] if not x["settled"]]
    rows_o = "".join(
        f"<tr><td>{esc(x['title'])}</td><td>{x['side']} @ {x['entry_c']}c</td>"
        f"<td>{x['base_pct']}%</td><td>{x['gap_pts']}pts</td><td>{x['ts']}</td></tr>"
        for x in open_sigs) or "<tr><td colspan=5>no open paper signals</td></tr>"
    rows_s = "".join(
        f"<tr><td>{esc(x['title'])}</td><td>{x['side']} @ {x['entry_c']}c</td>"
        f"<td>{'WIN' if x['won'] else 'LOSS'}</td><td>{x['pnl_c']:+}c</td><td>{x['ts']}</td></tr>"
        for x in ECON_STATE["settled"]) or "<tr><td colspan=5>nothing settled yet</td></tr>"
    rows_m = "".join(
        f"<tr><td>{esc(m['title'])}</td><td>{m['mid_c']}c</td>"
        f"<td>{m['base'] if m['base'] is not None else 'n/a'}%</td><td>{m['expires']}</td></tr>"
        for m in ECON_STATE["markets"]) or "<tr><td colspan=4>no econ markets found</td></tr>"
    return f"""<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Econ Paper Scanner</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:12px;background:#0d1117;color:#e6edf3}}
h2{{font-size:1.05em;margin:18px 0 6px}}
table{{border-collapse:collapse;width:100%;font-size:.82em}}
td,th{{border:1px solid #30363d;padding:5px 6px;text-align:left;vertical-align:top}}
th{{background:#161b22}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;margin-bottom:8px}}
</style></head><body>
<div class="card"><b>Econ Base-Rate Scanner</b> &nbsp; PAPER ONLY - no orders, no money<br>
last scan: {ECON_STATE['last_scan'] or 'never'} &nbsp;
series: {len(ECON_STATE['series_found'])} &nbsp;
unparsed: {ECON_STATE['unparsed']} &nbsp;
error: {esc(ECON_STATE['last_error'] or '-')}<br>
<b>paper record: {w}W-{n-w}L ({round(100*w/n,1) if n else 0}%), {tot:+.0f}c</b>
<br><small>auto-refreshes every 5 min</small></div>
<h2>Open paper signals</h2>
<table><tr><th>Market</th><th>Paper trade</th><th>Base rate</th><th>Gap</th><th>UTC</th></tr>{rows_o}</table>
<h2>Settled paper trades (last 25)</h2>
<table><tr><th>Market</th><th>Paper trade</th><th>Result</th><th>P&L</th><th>UTC</th></tr>{rows_s}</table>
<h2>Econ market inventory</h2>
<table><tr><th>Market</th><th>Mid</th><th>Base rate</th><th>Expires</th></tr>{rows_m}</table>
</body></html>"""

# ============================================================ PATCH 9 -----
# Backtest v2 data export (READ-ONLY). Pulls settled 15-min markets plus
# their 1-minute candlesticks from Kalshi (live tier, ~3-month window) so
# the full stack can be replayed against REAL quotes instead of assumed
# spreads. Guarded by EXPORT_KEY env; no orders, no money touched.
EXPORT_KEY = env("EXPORT_KEY", default="")
EXPORT_DIR = "/tmp/bt_export"
EXPORT_STATE = {"running": False, "series": "", "done": 0, "total": 0,
                "started": None, "error": None, "files": []}

def _export_ok(key: str) -> bool:
    return bool(EXPORT_KEY) and key == EXPORT_KEY

async def _export_series(series: str, days: int, kind: str = ""):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    # 1) all settled markets for the series (cursor pagination)
    markets, cursor = [], ""
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        d = await kalshi_get("/markets", params=params)
        batch = d.get("markets", [])
        markets += [m for m in batch
                    if m.get("result") in ("yes", "no")
                    and (not kind or m.get("strike_type") == kind)
                    and m.get("close_time", "") >= since.isoformat().replace("+00:00", "Z")[:19] + "Z"]
        cursor = d.get("cursor") or ""
        if not cursor or not batch:
            break
        await asyncio.sleep(0.1)
    EXPORT_STATE["total"] += len(markets)
    fname = f"{series}_{days}d.ndjson"
    path = os.path.join(EXPORT_DIR, fname)
    sem = asyncio.Semaphore(4)
    with open(path, "w") as fh:
        for m in markets:
            ticker = m.get("ticker", "")
            try:
                ot = int(datetime.fromisoformat(m["open_time"].replace("Z", "+00:00")).timestamp())
                ct = int(datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
                async with sem:
                    cd = await kalshi_get(
                        f"/series/{series}/markets/{ticker}/candlesticks",
                        params={"start_ts": ot - 3600, "end_ts": ct + 60,
                                "period_interval": 1})
                    await asyncio.sleep(0.08)
                rec = {"ticker": ticker, "result": m.get("result"),
                       "strike": m.get("floor_strike"),
                       "open_time": m.get("open_time"), "close_time": m.get("close_time"),
                       "candles": cd.get("candlesticks", [])}
                fh.write(json.dumps(rec) + "\n")
            except Exception as e:
                fh.write(json.dumps({"ticker": ticker, "error": str(e)[:120]}) + "\n")
            EXPORT_STATE["done"] += 1
    EXPORT_STATE["files"].append(fname)

async def _export_run(series_list, days, kind=""):
    EXPORT_STATE.update(running=True, done=0, total=0, error=None,
                        files=[], started=datetime.now(timezone.utc).isoformat())
    try:
        for s in series_list:
            EXPORT_STATE["series"] = s
            await _export_series(s, days, kind)
    except Exception as e:
        EXPORT_STATE["error"] = str(e)[:200]
        log.warning("export failed: %s", e)
    finally:
        EXPORT_STATE["running"] = False

@app.get("/export/start")
async def export_start(series: str = "KXBTC15M,KXETH15M", days: int = 30, key: str = "", kind: str = ""):
    if not _export_ok(key):
        return {"ok": False, "error": "disabled or bad key (set EXPORT_KEY in Railway)"}
    if EXPORT_STATE["running"]:
        return {"ok": False, "error": "already running", "state": EXPORT_STATE}
    days = max(1, min(int(days), 90))
    series_list = [s.strip().upper() for s in series.split(",") if s.strip()]
    asyncio.create_task(_export_run(series_list, days, kind))
    return {"ok": True, "series": series_list, "days": days, "kind": kind or "all"}

@app.get("/export/status")
async def export_status(key: str = ""):
    if not _export_ok(key):
        return {"ok": False, "error": "disabled or bad key"}
    return {"ok": True, "state": EXPORT_STATE}

@app.get("/export/download/{fname}")
async def export_download(fname: str, key: str = ""):
    if not _export_ok(key):
        return {"ok": False, "error": "disabled or bad key"}
    if "/" in fname or ".." in fname:
        return {"ok": False, "error": "bad name"}
    path = os.path.join(EXPORT_DIR, fname)
    if not os.path.exists(path):
        return {"ok": False, "error": "no such file",
                "files": EXPORT_STATE["files"]}
    return FileResponse(path, filename=fname)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

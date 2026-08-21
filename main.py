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
"""
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
    
    # Pull klines (reuses existing helper)
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
            p = prob_above(spot, strike, sigma, drift, t_eff)
            r15_bt = math.log(closes[d] / closes[d - WINDOW])
            p = recalibrate(p, m_left, r15_bt, sigma, symbol)
            
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
            if abs(p - 0.5) < 0.02:
                continue
            fee_c = kalshi_taker_fee_cents(entry_c) if fee else 0.0
            if side == "yes":
                pnl = (100.0 - entry_c) if win else -entry_c
            else:
                pnl = (100.0 - entry_c) if not win else -entry_c
            pnl -= fee_c
            all_pnls.append(round(pnl / 100.0, 4))  # convert cents -> dollars
    
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

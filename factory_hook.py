"""factory_hook.py — report-only integration hook to the trading-factory service.

DEPENDENCY NOTE: this module requires ``psycopg2-binary``.
It is intentionally NOT added to requirements.txt in this change — adding it
is a separate, manual deploy step. Until it is installed, every function in
this module is a safe no-op (the import is guarded).

WHAT THIS IS:
    A lightweight, synchronous, fire-and-forget reporter that mirrors the
    bot's trading activity into the trading-factory service's Postgres
    ledger (signals / fills / daily_perf tables). It is strictly READ/WRITE
    to the factory's *ledger only* — the factory never sends orders and this
    module never touches the execution path.

SAFETY GUARANTEES:
    - If FACTORY_DATABASE_URL is unset, every function is a no-op
      (returns None / False) and the bot behaves exactly as before.
    - Every function is wrapped internally in try/except and will NEVER
      raise into the caller. Still, wrap call sites in try/except anyway.
    - Never ``await`` on these calls in the order path. They are
      synchronous and fast, but treat them strictly as fire-and-forget.

USAGE (where to call in main.py):

    import factory_hook

    # 1) Right AFTER the F6 risk gate passes and the order intent is formed
    #    (BEFORE placing the Kalshi order is fine — this only logs the
    #    signal; it must not gate or delay the order):
    try:
        signal_id = factory_hook.report_signal(
            config_name=STRATEGY_NAME,       # must match strategy_configs.name
            symbol=ticker,
            direction="yes",                  # or "no"
            entry_price=price,
            model_prob=model_prob,
            market_price=market_price,
            size=contracts,
        )
    except Exception:
        signal_id = None

    # 2) In the fill/settlement handler, AFTER Kalshi confirms settlement:
    try:
        factory_hook.report_fill(
            config_name=STRATEGY_NAME,
            symbol=ticker,
            pnl=realized_pnl,
            brier_score=brier,               # or None
            signal_id=signal_id,             # or None -> execution_grade 'B'
        )
    except Exception:
        pass

    # 3) Optional: in a /health-style endpoint:
    #    factory_ok = factory_hook.heartbeat(STRATEGY_NAME)
"""

import os
from typing import Optional

DATABASE_URL = os.environ.get("FACTORY_DATABASE_URL")

try:
    import psycopg2
    import psycopg2.extras
except Exception:  # psycopg2 not installed yet -> everything is a no-op
    psycopg2 = None


def _connect():
    """Return a new connection, or None if the hook is disabled."""
    if not DATABASE_URL or psycopg2 is None:
        return None
    return psycopg2.connect(DATABASE_URL)


def _get_config_id(cur, config_name: str) -> Optional[str]:
    """Resolve strategy_configs.id by name. Returns None if not found."""
    cur.execute("SELECT id FROM strategy_configs WHERE name = %s", (config_name,))
    row = cur.fetchone()
    return str(row[0]) if row else None


def report_signal(
    config_name: str,
    symbol: str,
    direction: str,
    entry_price: float,
    model_prob: float,
    market_price: float,
    size: float,
) -> Optional[str]:
    """Insert a live-mode signal row. Returns the signal id, or None.

    Silently skips (returns None) if the hook is disabled or the config
    name is not found in strategy_configs. NEVER raises.
    """
    try:
        conn = _connect()
        if conn is None:
            return None
        try:
            with conn:
                with conn.cursor() as cur:
                    config_id = _get_config_id(cur, config_name)
                    if config_id is None:
                        return None
                    cur.execute(
                        """
                        INSERT INTO signals
                            (config_id, mode, symbol, direction, entry_price,
                             size, model_prob, market_price)
                        VALUES (%s, 'live', %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            config_id,
                            symbol,
                            direction,
                            entry_price,
                            size,
                            model_prob,
                            market_price,
                        ),
                    )
                    return str(cur.fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return None


def report_fill(
    config_name: str,
    symbol: str,
    pnl: float,
    brier_score: Optional[float],
    signal_id: Optional[str] = None,
) -> bool:
    """Insert a live-mode fill row and upsert today's daily_perf row.

    execution_grade is computed simply: 'A' if signal_id is present, else 'B'.
    daily_perf for today: trades+1, wins+1 if pnl>0, pnl+=pnl.
    Returns True on success, False otherwise. NEVER raises.
    """
    try:
        conn = _connect()
        if conn is None:
            return False
        try:
            with conn:
                with conn.cursor() as cur:
                    config_id = _get_config_id(cur, config_name)
                    if config_id is None:
                        return False
                    cur.execute(
                        """
                        INSERT INTO fills
                            (signal_id, config_id, mode, pnl, brier_score,
                             execution_grade)
                        VALUES (%s, %s, 'live', %s, %s, %s)
                        """,
                        (
                            signal_id,
                            config_id,
                            pnl,
                            brier_score,
                            "A" if signal_id else "B",
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO daily_perf (config_id, date, trades, wins, pnl)
                        VALUES (%s, CURRENT_DATE, 1, %s, %s)
                        ON CONFLICT (config_id, date) DO UPDATE SET
                            trades = daily_perf.trades + 1,
                            wins = daily_perf.wins + EXCLUDED.wins,
                            pnl = daily_perf.pnl + EXCLUDED.pnl
                        """,
                        (config_id, 1 if pnl > 0 else 0, pnl),
                    )
            return True
        finally:
            conn.close()
    except Exception:
        return False


def heartbeat(config_name: str) -> bool:
    """Verify the config exists in strategy_configs (for /health checks).

    Inserts/updates nothing. Returns True if the config row exists,
    False otherwise. NEVER raises.
    """
    try:
        conn = _connect()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                return _get_config_id(cur, config_name) is not None
        finally:
            conn.close()
    except Exception:
        return False

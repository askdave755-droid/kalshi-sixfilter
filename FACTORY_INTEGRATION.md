# Factory Integration (report-only)

`factory_hook.py` mirrors the bot's trading activity into the companion
**trading-factory** service's Postgres ledger. It is strictly a reporter:
the factory is **read-only** with respect to trading — it **never sends
orders**, and this hook never touches the execution path.

## Setup (Railway)

1. Add `psycopg2-binary` to `requirements.txt` (manual step — deliberately
   not included in the hook PR).
2. Set the environment variable in the Railway service settings:

   ```
   FACTORY_DATABASE_URL=postgresql://user:pass@host:5432/factory
   ```

3. Redeploy. If the env var is unset (or psycopg2 is missing), every hook
   function is a no-op returning `None`/`False` and the bot runs identically.

## Call sites in `main.py`

1. **`report_signal(...)`** — call right **after the F6 risk gate passes
   and the order intent is formed**. Returns a signal id to keep for the
   fill report. Fire-and-forget: wrap in `try/except`, never `await` it,
   never let it gate or delay the order.
2. **`report_fill(...)`** — call in the **fill/settlement handler, after
   Kalshi confirms settlement**, with the realized PnL (and Brier score /
   signal id if available). Also upserts today's `daily_perf` row.

Optional: `heartbeat(config_name)` for a `/health`-style check that the
config row exists in `strategy_configs`.

## Safety guarantees

- No changes to `main.py` in this PR — the hook is opt-in at the two call
  sites above.
- All functions are synchronous, internally wrapped in `try/except`, and
  **never raise** into the caller.
- Disabled by default: no env var => zero behavior change.
- Unknown `config_name` (not present in `strategy_configs`) => silently
  skipped.
- Ledger writes only: `signals`, `fills`, `daily_perf` (mode `'live'`).
  The factory never sends orders.

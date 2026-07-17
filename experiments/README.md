# Experiments

Reproducible scripts behind the v2 claims. Nothing in `src/` depends on this
directory.

- `sweep.py` — plateau-robustness analysis: perturbs one default parameter at a
  time and reports walk-forward OOS performance for each value. A parameter set
  worth keeping sits on a *plateau*, not a spike. Run on real data:

  ```bash
  python scripts/download_data.py --symbol NEARUSDT --days 90
  python experiments/sweep.py --csv data/klines_1m.csv
  ```

- `make_fixtures.py` / `parity_check.js` — pins the web simulator's JavaScript
  engine (`docs/engine.js`) to the Python engine: identical trades, prices,
  weights and metrics on shared fixture data across several parameter, cost and
  aggregation sets (including zero costs). Regenerate the fixtures and rerun:

  ```bash
  python experiments/make_fixtures.py
  node experiments/parity_check.js
  ```

  Both run in CI on every push.

## The research run (what the v2 defaults are based on)

91 days of real NEARUSDT 1m klines (2026-04-17 → 2026-07-16, from
data.binance.vision), aggregated to 60m:

| experiment | result (walk-forward OOS, net) |
|---|---|
| v1 scalper @ 4m (the old bot, its own validator) | −82.4% (714 trades) |
| v1 scalper @ 60m | −9.4% (39 trades) |
| v2, top-1 selection | +14.1%, Sharpe 3.58 |
| v2, top-3 ensemble | +14.5%, Sharpe 3.82 (research loop) |
| v2, top-3, 2× fees + slippage | +13.3%, Sharpe 3.54 |
| v2, top-3, ETHUSDT (held-out symbol) | +5.8% |
| v2, top-3, SOLUSDT (held-out symbol, downtrend) | +0.3% (was −5.9% without the trend filter) |

The packaged engine's slightly different (exact per-bar) accounting reports
+10.7% OOS on the same data — same trades, more precise bookkeeping. Plateau
table: docs/STRATEGY.md §7.

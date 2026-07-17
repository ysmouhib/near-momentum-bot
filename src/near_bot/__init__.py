"""near_bot v2: a cost-aware, volatility-targeted trend framework for Binance.

v2 is a ground-up rewrite of the v1 momentum scalper after its walk-forward
out-of-sample result on real data was unambiguous: -82% over 91 days, with a
negative *gross* expectancy — the 4-minute edge never existed, and 0.24%
round-trip costs made sure of the rest.

The v2 design (see docs/STRATEGY.md for the full reasoning):

- **Score, not checklist** — a continuous conviction score blending
  vol-normalised time-series momentum, Donchian breakout position, and
  smoothed taker-imbalance order flow.
- **Regime gates** — Kaufman efficiency-ratio chop filter + a long-term trend
  filter. You cannot buy your way out of a downtrend; you can decline to try.
- **Hysteresis** — enter above theta_in, exit below theta_out; the no-trade
  band in between is where expected edge is smaller than round-trip cost.
- **Volatility targeting** — position size makes the position's annualised
  vol equal the target, so risk is stable across regimes.
- **Slower timeframe** — 60m bars by default, where the cost burden is an
  order of magnitude smaller relative to the moves being captured.
- **Walk-forward with top-k parameter ensembling and Sharpe selection** —
  the only performance number this project endorses.

Nothing here is investment advice. See the README disclaimer.
"""

__version__ = "2.0.0"

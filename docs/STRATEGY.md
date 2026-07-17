# Strategy v2: score-ensemble trend following with volatility targeting

This document is the full reasoning behind v2: the failure analysis of v1 on real
data, the design that the evidence forced, and the validation protocol. It is
written to be argued with — every claim is reproducible from this repo.

## 1. Why v1 had to die

v1 was a 4-minute momentum scalper: six AND-ed binary conditions (MA stack, rising
close, volume spike, taker-buy ratio > 0.55, RSI band, ATR floor, cost gate) with
ATR-scaled exits. It was engineered honestly — no look-ahead, fees on every fill,
walk-forward validation. Run on **91 days of real NEARUSDT data** (April → July
2026), its own validator reported:

```
v1 @ 4m,  walk-forward OOS: 714 trades, win rate 23.8%,
           expectancy −0.24%/trade net (−0.04% gross), total −82.4%
v1 @ 60m, walk-forward OOS:  39 trades, expectancy −0.23%/trade net, total −9.4%
```

Two findings, both fatal:

1. **The gross expectancy was negative.** Before costs, the signal had no edge at
   all on real data. No exit scheme or position sizing fixes a signal that loses
   money for free.
2. **The cost drag was 0.20% per trade.** At 4-minute bars, trades last ~12
   minutes and targets are a few tenths of a percent; a 0.24% round-trip cost is
   the whole ballgame. v1's own cost gate (TP ≥ 1.5× round-trip cost) correctly
   identified the problem and could not solve it: targeting 0.36% moves with
   0.24% costs still leaves a coin-flip for crumbs.

The scalping design was not *unlucky*. It was structurally doomed: **costs are
fixed per trade; capturable trends scale with holding time.** The only way out is
slower, bigger trades.

## 2. The v2 hypothesis

Three claims, each independently testable:

1. **Time-series momentum at multi-day horizons has a real, positive premium in
   crypto** (consistent with the published TSMOM literature across asset classes),
   large enough per trade to clear 0.24% round-trip costs many times over.
2. **Regime filters are where the actual alpha lives**: trend following makes
   money in trends and gives it back in chop — so the highest-value decision is
   *not trading* when price action is noise or the trend is down.
3. **A continuous score with hysteresis loses less to costs than a binary
   checklist**, because it only changes position when the evidence changes
   meaningfully.

## 3. The signal: one score in [−1, 1]

Three independent families of evidence, each volatility-normalised, blended:

```
mom     = 0.5·tanh(TSMOM_fast) + 0.5·tanh(TSMOM_slow)   # trend persistence
bo      = Donchian position in [−1,1]                    # range expansion
ofi     = smoothed taker-imbalance z-score ÷ 3           # order flow
score   = clip(w_mom·mom + w_bo·bo + w_ofi·ofi, −1, +1)
```

* **TSMOM(k)** = `ret(k) / (σ_bar · √k)` — the k-bar return in units of its own
  noise. +1 means "one sigma of trend". Fast (36h ≈ 1.5 days) and slow (120h ≈ 5
  days) are averaged so no single lookback can dominate.
* **Donchian position** — where the close sits inside the trailing 48-bar range.
  Breakouts are how trends announce themselves; a continuous position beats a
  one-off breakout event.
* **Order-flow z-score** — Binance klines expose `taker_buy_volume`; the EMA of
  the signed imbalance, divided by its own dispersion, extracts persistent
  aggressive buying. Kept from v1: it is the only genuine microstructure
  information available for free. (Ablation: it contributes modestly — the blend
  works without it, slightly better with it.)

Two **gates** zero the score (they decide *whether* to trade, not how much to
believe the signal):

* **Chop gate** — Kaufman Efficiency Ratio `ER = |net move| / path length` over
  48 bars must be ≥ 0.20. Below that, the market is mostly back-and-forth and a
  trend system is paying fees to a random walk.
* **Trend filter** — close must be above the 200-bar SMA (≈ 8 days). Long-only
  spot cannot profit from downtrends; the filter simply declines to try. On the
  held-out SOL sample (a falling market) this single gate moved OOS from −5.9%
  to +0.3%.

## 4. Hysteresis: the cost-aware no-trade band

Entry requires `score > θ_in` (0.25). Exit on `score < θ_out` (0.0) — a *lower*
bar. The band between them is where the expected edge is smaller than round-trip
cost, so the position is left alone. This is the transaction-cost literature's
classic no-trade region, and it is what lets a continuous signal be traded
without churning: the score can wobble around zero all day without generating a
single fee.

Exits are the first of: score fade, a **chandelier trailing stop** (peak close
minus 3×ATR — lets winners run with a volatility-scaled leash), or a **time
stop** (168 bars = 1 week; stale positions free the capital). No fixed
take-profit: capping winners is how trend systems die.

## 5. Volatility-targeted sizing

```
weight = min(1, target_vol / (σ_bar · √(bars_per_year)))
```

A 40% annualised vol budget, capped at 100% notional (spot, no leverage), with a
5% minimum-weight dust filter. Quiet markets get bigger positions, wild markets
smaller ones — risk per trade is roughly constant across regimes, which is what
makes Sharpe comparisons between periods meaningful. The weight is fixed at
entry and not adjusted intra-trade (fewer trades, simpler accounting).

## 6. Backtest methodology and the biases it avoids

* **No look-ahead.** Signals on closed bars only; fills at the *next* bar's open.
  The unfinished aggregated bar is dropped. A unit test mutates the future and
  demands identical past trades.
* **Costs everywhere.** 0.10% taker + 0.02% slippage per side by default; a test
  asserts doubling costs never helps.
* **Per-bar mark-to-market.** Equity, drawdown, Sharpe and exposure are computed
  bar-by-bar including open positions — not from a trade list that hides the
  ride. A hand-computed test pins the exact accounting (entry bar open→close,
  holding bars close→close, exit bar close→open, `w·fee` on each fill bar).
* **End-of-data positions closed** at the last bar (marked-to-market), never
  silently dropped.

What it still does not model: partial fills, order-book depth, latency, and your
own market impact. Treat results as an upper bound.

## 7. Validation protocol

* **Walk-forward.** Rolling 30-day train / 10-day test windows. In-sample
  ranking by per-bar **Sharpe** (not expectancy — it crowns variance), with a
  3-trade minimum. Only OOS windows are reported.
* **Top-3 ensemble.** Each window trades the three best in-sample combos and
  averages their per-bar returns. The single best combo is mostly luck; the
  ensemble trades away that selection variance. On real data it doubled the OOS
  trade count while raising OOS Sharpe.
* **Held-out symbols.** ETH and SOL were never used to pick defaults; they show
  +5.8% and +0.3% OOS respectively — the design generalises, the strength is
  asset- and phase-dependent.
* **Cost stress.** At 2× fees+slippage the NEAR OOS result barely moved
  (+13.3% vs +14.5% in the research run): the edge is not riding the cost
  assumption.
* **Plateau check.** Every default was perturbed one at a time (research run on
  real NEAR data, walk-forward OOS):

  | knob | values tried | OOS return range |
  |---|---|---|
  | trend_window | 100 / 150 / 200 / 250 / 300 | +15% … +18% |
  | mom_slow | 72 / 96 / 120 / 168 | +14% … +17% |
  | mom_fast | 24 / 36 / 48 | +12% … +16% |
  | theta_out | −0.1 / 0.0 / +0.1 | +9% … +14% |
  | w_ofi | 0.0 / 0.2 / 0.35 | +13% … +14% |
  | target_vol | 0.25 / 0.40 / 0.55 / 0.70 | +9% … +26% (return scales, Sharpe flat) |

  A flat plateau, not a spike — the defaults are not a lucky point.

## 8. Honest limitations

1. **90 days is one market phase**, and it trended. Buy & hold beat the strategy
   on raw OOS return (+38% vs +11%) — with far deeper drawdowns and 100% exposure
   versus 30%. Whether that trade-off is attractive depends on what you fear.
2. **Long-only.** Downtrends are dodged, not harvested. A USD-M futures broker
   (roadmap) would unlock the symmetric short side.
3. **Selection bias is reduced, not eliminated.** The grid, the window sizes,
   the timeframe and the strategy family itself were all chosen by a human who
   had seen the data. The defence is the held-out symbols, the cost stress, the
   plateau check — and rerunning everything on fresh data, which is two commands.
4. **Execution reality**: partial fills, book depth and latency are unmodelled.
   Paper-trade on the testnet before believing anything.

## 9. Not financial advice

Educational research framework. Backtested and out-of-sample simulated
performance does not predict future results. Trade the testnet first; never risk
money you cannot afford to lose.

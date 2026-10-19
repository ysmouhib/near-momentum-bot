# Plateau hunt (pre-registered: experiments/plateau_hunt.py)

- Code state at run time: 8adcfb6 (clean) (spec and trials-ledger charge committed before this run)
- Search span: [2021-01-01, 2025-01-01) (2021-2024), symbols NEARUSDT, ETHUSDT, SOLUSDT, 60m bars, fixed-config backtests, costs 0.100% fee + 0.020% slippage per side
- Grid: 125 cells — theta_in [0.15, 0.2, 0.25, 0.3, 0.35]; trail_atr [2.0, 2.5, 3.0, 3.5, 4.0]; er_min [0.1, 0.15, 0.2, 0.25, 0.3] (all other parameters at their defaults)
- Score: mean over symbols of the per-bar Sharpe, net of costs; eligible = >= 30 trades per symbol (15 per half)
- Selection: eligible cell with the best neighbourhood score (mean over itself and all cells within one grid step on every knob)
- Trials ledger: stats.TRIALS_TRIED = 219, every cell charged whatever it scored
- Data: full span, not the proposed 10% random-block subsample (decision 2026-10-19: ~0.25 s per config and symbol in full; the subsample would have cut evidence ~10x and forced long holds flat at every interval end). Neighbourhood averaging is the mitigation for ranking noise, by design.
- Holdout 2025-01-01 -> 2026-09-30: NOT touched by this run; status "viewed in aggregate in e0d6f3c"

## (a) Plateau map, 2021-2024

Neighbourhood score (raw score in brackets), annualised Sharpe; `x` = ineligible (< min trades), **bold** = selected cell.

**er_min = 0.1**

| theta_in \ trail_atr | 2.0 | 2.5 | 3.0 | 3.5 | 4.0 |
|---|---|---|---|---|---|
| 0.15 | +0.53 (+0.51) | +0.59 (+0.54) | +0.68 (+0.70) | +0.77 (+0.76) | **+0.79 (+0.83)** |
| 0.2 | +0.51 (+0.44) | +0.58 (+0.45) | +0.66 (+0.63) | +0.76 (+0.72) | +0.79 (+0.78) |
| 0.25 | +0.46 (+0.43) | +0.54 (+0.43) | +0.63 (+0.66) | +0.73 (+0.74) | +0.76 (+0.79) |
| 0.3 | +0.43 (+0.38) | +0.51 (+0.41) | +0.61 (+0.63) | +0.72 (+0.70) | +0.75 (+0.75) |
| 0.35 | +0.42 (+0.36) | +0.50 (+0.40) | +0.60 (+0.63) | +0.71 (+0.69) | +0.73 (+0.78) |

**er_min = 0.15**

| theta_in \ trail_atr | 2.0 | 2.5 | 3.0 | 3.5 | 4.0 |
|---|---|---|---|---|---|
| 0.15 | +0.46 (+0.59) | +0.53 (+0.58) | +0.61 (+0.81) | +0.70 (+0.84) | +0.72 (+0.82) |
| 0.2 | +0.45 (+0.55) | +0.52 (+0.53) | +0.60 (+0.77) | +0.69 (+0.80) | +0.72 (+0.80) |
| 0.25 | +0.42 (+0.52) | +0.49 (+0.49) | +0.58 (+0.72) | +0.67 (+0.79) | +0.70 (+0.76) |
| 0.3 | +0.39 (+0.48) | +0.47 (+0.44) | +0.57 (+0.70) | +0.67 (+0.76) | +0.69 (+0.72) |
| 0.35 | +0.38 (+0.45) | +0.46 (+0.44) | +0.56 (+0.70) | +0.66 (+0.74) | +0.68 (+0.73) |

**er_min = 0.2**

| theta_in \ trail_atr | 2.0 | 2.5 | 3.0 | 3.5 | 4.0 |
|---|---|---|---|---|---|
| 0.15 | +0.30 (+0.34) | +0.38 (+0.34) | +0.50 (+0.51) | +0.61 (+0.60) | +0.64 (+0.54) |
| 0.2 | +0.30 (+0.34) | +0.38 (+0.34) | +0.49 (+0.51) | +0.60 (+0.60) | +0.63 (+0.55) |
| 0.25 | +0.28 (+0.33) | +0.36 (+0.34) | +0.48 (+0.52) | +0.59 (+0.62) | +0.62 (+0.54) |
| 0.3 | +0.26 (+0.30) | +0.35 (+0.32) | +0.48 (+0.52) | +0.59 (+0.59) | +0.62 (+0.53) |
| 0.35 | +0.25 (+0.24) | +0.35 (+0.31) | +0.47 (+0.54) | +0.59 (+0.58) | +0.62 (+0.55) |

**er_min = 0.25**

| theta_in \ trail_atr | 2.0 | 2.5 | 3.0 | 3.5 | 4.0 |
|---|---|---|---|---|---|
| 0.15 | +0.17 (+0.07) | +0.27 (-0.07) | +0.42 (+0.35) | +0.56 (+0.58) | +0.61 (+0.47) |
| 0.2 | +0.17 (+0.08) | +0.27 (-0.06) | +0.42 (+0.35) | +0.56 (+0.58) | +0.61 (+0.48) |
| 0.25 | +0.17 (+0.07) | +0.27 (-0.05) | +0.42 (+0.35) | +0.56 (+0.58) | +0.61 (+0.48) |
| 0.3 | +0.16 (+0.07) | +0.26 (-0.04) | +0.43 (+0.35) | +0.57 (+0.57) | +0.62 (+0.48) |
| 0.35 | +0.16 (+0.07) | +0.26 (-0.02) | +0.43 (+0.40) | +0.57 (+0.61) | +0.62 (+0.56) |

**er_min = 0.3**

| theta_in \ trail_atr | 2.0 | 2.5 | 3.0 | 3.5 | 4.0 |
|---|---|---|---|---|---|
| 0.15 | +0.09 (+0.18) | +0.20 (+0.16) | +0.39 (+0.51) | +0.56 (+0.80) | +0.63 (+0.64) |
| 0.2 | +0.09 (+0.19) | +0.21 (+0.17) | +0.39 (+0.52) | +0.56 (+0.81) | +0.63 (+0.65) |
| 0.25 | +0.09 (+0.18) | +0.21 (+0.17) | +0.39 (+0.51) | +0.57 (+0.81) | +0.63 (+0.65) |
| 0.3 | +0.09 (+0.17) | +0.21 (+0.16) | +0.40 (+0.51) | +0.58 (+0.81) | +0.65 (+0.70) |
| 0.35 | +0.09 (+0.16) | +0.21 (+0.15) | +0.40 (+0.50) | +0.58 (+0.80) | +0.66 (+0.71) |

## Selected plateau center

- **theta_in=0.15, trail_atr=4.0, er_min=0.1**
- Neighbourhood score +0.79, raw score +0.83 (annualised Sharpe)
- Per symbol: NEARUSDT Sharpe +0.54, 274 trades; ETHUSDT Sharpe +0.46, 277 trades; SOLUSDT Sharpe +1.50, 263 trades
- On the grid boundary in theta_in, trail_atr, er_min: the plateau may extend beyond the tested range. The grid is not extended, by rule.

## Selected cell, pooled equal-weight portfolio, 2021-2024 (in-sample)

This is the sample the cell was selected on. The deflated Sharpe charges for the selection (N from the ledger, V from all cells' pooled Sharpes on the same bars); only the holdout can confirm anything.

### theta_in=0.15, trail_atr=4.0, er_min=0.1

- OOS bars 35,050, trades 814, **independent decisions N_eff = 256**
- Sharpe (ann.): +1.13  95% CI [+0.15, +2.07]
- Annualised return: +25.78%  95% CI [+0.34%, +60.09%]
- Max drawdown: -29.90%  95% CI [-44.08%, -15.96%]
- Total return: +150.32%
- Expectancy / decision: +0.411%  95% CI [+0.015%, +0.831%]
- Deflated Sharpe: P(SR > SR*) = 0.695, excess over benchmark +0.26 (SR* +0.87 ann., N trials 219)
- **Verdict: CIs exclude zero but P(SR > SR*) = 0.69 < 0.95: not established after charging for 219 configurations tried**

## (b) Stability check: 2021-22 vs 2023-24

| half | selected cell raw score | its neighbourhood score | rank (share of eligible cells ahead) | the half's own center |
|---|---|---|---|---|
| 2021-2022 | +0.61 | +0.60 | 0% | theta_in=0.15, trail_atr=4.0, er_min=0.1 |
| 2023-2024 | +1.05 | +0.98 | 9% | theta_in=0.3, trail_atr=4.0, er_min=0.1 |

- Rank correlation of neighbourhood scores between the halves (Spearman, all 125 cells): +0.79

- (b) consistent with (a): the selected cell scores > 0 and ranks in the top 25% of eligible cells in both halves.

## Verdict

- In-sample (2021-2024), selected cell: CIs exclude zero but P(SR > SR*) = 0.69 < 0.95: not established after charging for 219 configurations tried
- Stability: consistent

**Overall: the selected cell survives the in-sample tests and the stability check. In-sample evidence only; the pre-registered holdout test (c) awaits approval.**

Next step (c), only after approval: `python experiments/plateau_hunt.py --holdout --center theta_in=0.15 trail_atr=4.0 er_min=0.1` — holdout "viewed in aggregate in e0d6f3c".

---

*Everything below was added on 2026-10-19, after the pre-registered run. Everything above is the unedited generated output of that run.*

## Post-hoc context: selected cell vs buy-and-hold, 2021-2024

> **Post-hoc, descriptive only.** Computed after the pre-registered run (4ecfe2c) by `experiments/plateau_context.py`; not part of the pre-registration and not used for selection or any pass/fail rule.

Same bars, same per-bar accounting. Selected cell: `theta_in=0.15, trail_atr=4, er_min=0.1`. Buy-and-hold = the coin's own close-to-close returns, fully invested; the pooled row rebalances to equal weight every bar.

| | Sharpe: buy-and-hold | Sharpe: selected cell | total return: buy-and-hold | total return: selected cell | max drawdown: buy-and-hold | max drawdown: selected cell | cell exposure | correlation |
|---|---|---|---|---|---|---|---|---|
| NEARUSDT | +0.89 | +0.54 | +238% | +62% | -95% | -49% | 38% | +0.59 |
| ETHUSDT | +0.87 | +0.46 | +355% | +43% | -81% | -41% | 41% | +0.52 |
| SOLUSDT | +1.58 | +1.50 | +12159% | +434% | -97% | -31% | 39% | +0.56 |
| Pooled (equal weight) | +1.32 | +1.13 | +2472% | +150% | -89% | -30% | 39% | +0.58 |

Exposure = share of bars with an open position; correlation = between the per-bar returns of the cell and of buy-and-hold.

## Finding

**On NEAR/ETH/SOL over 2021-2024, this engine family's parameter search converges toward market exposure and does not beat buy-and-hold.**

- The pre-registered selection landed on the loosest corner of the grid on all three knobs (lowest entry threshold, weakest chop filter, widest trailing stop): the score rises with time in the market, not with sharper timing.
- The selected cell failed its pre-registered deflated-Sharpe gate (P(SR > SR*) = 0.69 < 0.95 after charging for 219 configurations). The generated "Overall" line above says it "survives the in-sample tests"; that wording overstates it — its CIs exclude zero, but the DSR gate is not passed.
- Against buy-and-hold on the same bars it has a lower Sharpe on every symbol (pooled +1.13 vs +1.32) and a far lower total return, and its returns correlate +0.58 with the market's. What it does deliver is a much shallower drawdown (pooled -30% vs -89%) at about 39% exposure — a partially invested version of the same market exposure, which is not evidence of timing skill.

**Decision (2026-10-19): step (c) is not run; the holdout stays sealed.** The holdout [2025-01-01, 2026-10-01) has not been used by any selection and is reserved for a future strategy family. Its status label stands — "viewed in aggregate in e0d6f3c" (the default engine's walk-forward results over those months were reported there) — so a future family should treat it as a viewed, not pristine, holdout.

Not financial advice.
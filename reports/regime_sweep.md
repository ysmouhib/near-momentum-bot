# Synthetic-regime sensitivity sweep (DESIGN.md §7.4)

- Data: `data/NEARUSDT_1m_20210101_20261001.csv`, NEARUSDT 60m bars; walk-forward train 90d / test 30d, top-3 ensemble (selection fixed from the engine-only run)
- Synthetic humans: 200 per accuracy, seed 7; full system = engine + risk overlay (kill switch, risk sizing, transition cooldown), overlay state restarted per window; a deployment starts DISARMED
- Labelled windows: 66 traded (24 good, 42 bad, of which 0 flat); windows the walk-forward skipped carry no trades and are left out

## Value of accuracy

Mean across synthetic humans, [5th, 95th] percentile in brackets. Rows 0% and 100% are the deterministic oracle bounds.

| p | total return | max drawdown | expectancy / trade | trades | armed |
|---|---|---|---|---|---|
| 0% | -31.80% [-31.80%, -31.80%] | -32.74% [-32.74%, -32.74%] | -0.067% | 588.0 | 64% |
| 50% | +3.12% [-9.09%, +18.35%] | -10.07% [-15.30%, -5.54%] | +0.007% | 431.9 | 49% |
| 60% | +12.72% [-0.81%, +29.44%] | -7.33% [-11.46%, -4.26%] | +0.031% | 398.2 | 47% |
| 70% | +20.54% [+7.65%, +34.35%] | -5.65% [-8.46%, -3.63%] | +0.051% | 378.2 | 44% |
| 80% | +29.94% [+17.22%, +41.14%] | -4.66% [-6.26%, -3.28%] | +0.076% | 355.4 | 42% |
| 100% | +46.85% [+46.85%, +46.85%] | -3.28% [-3.28%, -3.28%] | +0.129% | 304.0 | 36% |

## Break-even accuracy

- Empirical (line fit through the sweep's mean summed return): 45%
- Arithmetic on the same windows, |S_bad| / (S_good + |S_bad|): 44%
- Design formula L / (W + L) on measured per-trade W = +0.161%, L = -0.073%: 31%
- Theoretical (DESIGN.md §0, W = +0.9%, L = -0.6%): 40%
- The brief's figure (reference only): 56%

## Verdict

- Plumbing: consistent - the sweep's break-even 45% matches the arithmetic on the same windows 44% (within 5%; the residual is cooldown + sampling).
- p = 50%: total return +3.12% [-9.09%, +18.35%] across synthetic humans -> band includes zero: not distinguishable from noise
- p = 60%: total return +12.72% [-0.81%, +29.44%] across synthetic humans -> band includes zero: not distinguishable from noise
- p = 70%: total return +20.54% [+7.65%, +34.35%] across synthetic humans -> positive for 95% of them
- p = 80%: total return +29.94% [+17.22%, +41.14%] across synthetic humans -> positive for 95% of them

**Overall: a regime call accurate to >= 70% makes this system reliably positive on these windows. Whether a real human gets there is measured only by the journal.**

Caveat: synthetic labels give the human a clairvoyant-grade information basis. This sweep maps the value of accuracy; it does not certify that a real human achieves any of these accuracies. Only the journal (`near-bot journal report`) measures that.

Not financial advice.
# NEAR Momentum Bot v2 — a trend engine that survives its fees

A research-first trading framework for **NEAR/USDT on Binance spot**, rebuilt from
scratch around one hard-won finding: **on 91 days of real data, the v1 scalper lost
82% out-of-sample** — the 4-minute edge never existed, and 0.24% round-trip costs
made sure of the rest. v2 is what the evidence demanded: a slower, cost-aware,
volatility-targeted trend engine with honest validation.

[![CI](https://github.com/ysmouhib/near-momentum-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ysmouhib/near-momentum-bot/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Lint: ruff](https://img.shields.io/badge/lint-ruff-261230)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> **Not financial advice.** Educational research project. It defaults to the Binance
> **testnet** (paper trading). See the [disclaimer](#disclaimer).

## What's new in v2 (and why)

| v1 (the old bot) | v2 (this rewrite) |
|---|---|
| 4-minute bars | **60-minute bars** — costs are fixed per trade, trends scale with time |
| Six AND-ed binary gates | **Continuous conviction score**: vol-normalised TSMOM + Donchian breakout + smoothed taker-imbalance order flow |
| RSI band + volume spike | **Regime gates**: Kaufman efficiency-ratio chop filter + 200-bar trend filter |
| Entry/exit on the same threshold | **Hysteresis** (θ_in > θ_out): the no-trade band where edge < costs |
| Fixed quantity per trade | **Volatility-targeted sizing** (constant risk across regimes, capped at 100%) |
| Best-single-combo walk-forward | **Top-3 parameter ensemble**, Sharpe-based selection |
| Trade-list equity | **Per-bar mark-to-market** equity, exposure and drawdown |

The full reasoning — including the v1 failure analysis and the plateau-robustness
checks on every parameter — is in [`docs/STRATEGY.md`](docs/STRATEGY.md).

## Results, honestly

Real 91-day NEARUSDT data (April → July 2026), 60m bars, **0.10% taker fee + 0.02%
slippage per side on every fill**. Walk-forward re-optimises a deliberately small
grid on rolling 30-day windows and reports **only the unseen 10-day windows that
follow**.

| Configuration | OOS return | Sharpe | Max DD | Trades |
|---|---|---|---|---|
| v1 scalper @ 4m (the old bot) | **−82.4%** | — | −82.5% | 714 |
| v1 scalper @ 60m | −9.4% | — | −17.0% | 39 |
| **v2 @ 60m — walk-forward OOS** | **+10.7%** | **+2.84** | **−7.5%** | 38 |
| v2 @ 60m — 2× cost stress | +13.3% | +3.54 | −5.9% | 36 |
| v2 @ 60m — ETHUSDT (held-out symbol) | +5.8% | +2.15 | −7.5% | 26 |
| v2 @ 60m — SOLUSDT (held-out symbol) | +0.3% | +0.18 | −5.7% | 35 |

Two honest caveats, stated up front: (1) over those particular OOS windows
buy & hold made more raw return (+38.2%) with far larger drawdowns — the strategy
was invested only ~30% of the time, which is the trade-off for the −7.5% max DD;
(2) 90 days is one market phase, and NEAR trended well in it. That's why the held-out
symbols and the 2× cost stress are in the table, and why you should rerun this on
*fresh* data before believing it. The machinery to do so is the deliverable.

![Walk-forward out-of-sample on real data](docs/img/walkforward_real.png)

Every number above is reproducible in two commands — see below.

## Quickstart

```bash
git clone https://github.com/ysmouhib/near-momentum-bot.git
cd near-momentum-bot
pip install -e ".[dev,plot]"

# 1. Download 90 days of real 1m klines (no API key; Binance public dumps)
python scripts/download_data.py --symbol NEARUSDT --days 90

# 2. Fixed-config backtest (optimistic upper bound)
near-bot backtest --csv data/klines_1m.csv --trades --plot reports/backtest.png

# 3. Walk-forward validation (the number that actually matters)
near-bot walkforward --csv data/klines_1m.csv --plot reports/oos.png

# 4. (Optional) paper trade on the testnet
cp .env.example .env   # paste testnet keys from testnet.binance.vision
export $(cat .env | xargs)
near-bot test-connection
near-bot paper
```

A custom parameter grid for walk-forward can be supplied as YAML:

```yaml
# grid.yaml
theta_in: [0.15, 0.25, 0.35]
trail_atr: [2.5, 3.5]
er_min: [0.15, 0.25]
```

```bash
near-bot walkforward --csv data/klines_1m.csv --grid grid.yaml --top-k 3
```

## Try it in the browser

The GitHub Pages site ships a **client-side simulator**
([`docs/simulator.html`](docs/simulator.html)) that runs the exact engine on real
Binance history: public 1-minute klines are fetched straight from your browser (no
account or API key), every parameter is adjustable, and you can run either a
fixed-config backtest or full **walk-forward validation in-page**. The JavaScript
engine is pinned to the Python engine by an automated parity check
(`experiments/parity_check.js`) — identical trades on shared fixtures, verified in
CI. Where Binance's API is geo-blocked, upload a CSV written by
`scripts/download_data.py` instead.

## Architecture

```
1m klines (REST API or data.binance.vision dumps)
      → aggregate to N-minute bars → causal indicators → conviction score
                                                                │
                ┌───────────────────┬───────────────────────────┴──────────┐
                ▼                   ▼                                        ▼
        backtest engine      walk-forward validator                   live executor
   (hysteresis, vol-target,  (Sharpe selection, top-3              (testnet / live,
    per-bar MTM, fees)        ensemble, OOS-only report)            same score + sizing)
```

```
src/near_bot/
├── config.py       # typed config (YAML + env secrets)
├── data.py         # klines parsing, N-minute aggregation, dump-zip loading
├── indicators.py   # TSMOM, efficiency ratio, Donchian position, OFI z-score, ATR, vol
├── score.py        # the composite conviction score + regime gates
├── backtest.py     # hysteresis loop, vol-target sizing, per-bar MTM accounting
├── walkforward.py  # Sharpe selection, top-k ensemble, embargo option
├── report.py       # equity vs buy&hold, drawdown, exposure, return plots
├── dump_client.py  # data.binance.vision public dumps (no key, no geo-block)
├── broker.py       # Binance spot wrapper (paginated klines, lot-size, min-notional)
├── executor.py     # paper/live loop (same score, same sizing, real fill prices)
└── cli.py          # command-line entry point
```

Every trading decision flows through the same `score.compute()` +
`backtest.position_weight()` code, so what you backtest is what you run.

## Development

```bash
pytest -q                              # 32-test suite
ruff check src tests                   # lint
python experiments/make_fixtures.py    # regenerate parity fixtures
node experiments/parity_check.js       # JS engine == Python engine
```

CI runs the suite and linter on Python 3.10–3.12, plus the engine parity check,
on every push and pull request.

## Roadmap

- [x] Walk-forward validation with parameter ensembling
- [x] Volatility-targeted sizing and per-bar MTM equity
- [x] Regime gates (efficiency ratio + trend filter)
- [x] Browser walk-forward validation
- [ ] Multi-symbol portfolio mode with correlation-aware caps
- [ ] Binance USD-M futures broker for symmetric short signals
- [ ] Order-book-depth-aware slippage model

## Disclaimer

For educational purposes only. Nothing here is financial advice. Cryptocurrency
trading carries substantial risk of loss. Backtested and simulated results —
including out-of-sample ones — do not guarantee future performance. Use the
testnet, and never trade money you cannot afford to lose. The authors accept no
liability for any losses.

## License

[MIT](LICENSE)

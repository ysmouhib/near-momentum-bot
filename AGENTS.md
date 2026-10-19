# AGENTS.md — guidance for AI coding agents

## Project overview

**near-momentum-bot v2** is a research-first, cost-aware, volatility-targeted
trend-following framework for **NEAR/USDT on Binance spot** (long-only). It is
an educational project whose stated goal is honest validation methodology, not a
performance claim: on 91 days of real data the v1 4-minute scalper lost −82%
out-of-sample, and v2 is presented as *"not distinguishable from noise at this
sample size"* — deliberately so.

The core design (full reasoning in `docs/STRATEGY.md`):

- **Continuous conviction score in [-1, 1]**, blending vol-normalised
  time-series momentum (TSMOM fast/slow), Donchian breakout position, and a
  smoothed taker-imbalance z-score (order flow), weighted 0.50 / 0.30 / 0.20.
- **Regime gates** multiply tradability to zero: Kaufman efficiency-ratio chop
  filter (`er_min`) and a long SMA trend filter (no longs below the 200-bar MA).
- **Hysteresis**: enter when score > `theta_in` (0.25), exit when score <
  `theta_out` (0.0). The band between them is the cost-aware no-trade region.
- **Volatility-targeted sizing**: weight = `target_vol / annualised_vol`,
  capped at 100% notional, with a `min_weight` dust floor.
- **60-minute bars** by default (1m klines aggregated), chosen because fixed
  per-trade costs are an order of magnitude less burdensome at slow timeframes.
- **Walk-forward validation** with Sharpe-based selection and a top-3 parameter
  ensemble is the only performance number the project endorses.

Every trading decision flows through the same `score.compute()` +
`backtest.position_weight()` code for backtests, walk-forward and the live
executor — *what you backtest is what you run* is a load-bearing invariant.

**Not financial advice.** It defaults to the Binance testnet (paper trading).

## Technology stack and build

- **Python ≥ 3.10**, `src/` layout, packaged with **setuptools**
  (`pyproject.toml`). Console entry point: `near-bot = near_bot.cli:main`
  (also `python -m near_bot`).
- Core deps: `pandas>=2.0`, `numpy>=1.24`, `requests>=2.31`, `PyYAML>=6.0`.
  Optional extras: `[live]` = `python-binance` (broker wrapper, imported
  lazily so tests need no network), `[plot]` = `matplotlib` (report plots,
  also lazily imported), `[dev]` = `pytest`, `ruff`.
- A **JavaScript port of the engine** (`docs/engine.js`) powers the GitHub
  Pages browser simulator (`docs/simulator.html`). It must stay bit-compatible
  with the Python engine — see "Engine parity" below.

Setup:

```bash
pip install -e ".[dev,plot]"   # editable install with dev + plotting extras
```

## Repository layout

```
src/near_bot/
├── config.py       # typed dataclass Config (YAML + env secrets); ScoreParams/RiskParams/CostParams
├── data.py         # Binance kline parsing, N-minute aggregation (drops incomplete bar), dump-zip loading
├── indicators.py   # pure causal vectorised indicators: TSMOM, ER, Donchian, OFI z-score, ATR, vol; add_all(), warmup_bars()
├── score.py        # composite conviction score + regime gates; explain() one-liner
├── backtest.py     # event-driven loop, hysteresis, vol-target sizing, exact per-bar MTM accounting, metrics
├── walkforward.py  # rolling train/test windows, Sharpe selection, top-k ensemble, optional embargo; DEFAULT_GRID (12 combos, deliberately small)
├── report.py       # matplotlib equity/drawdown/exposure/trade plots (optional dep)
├── dump_client.py  # data.binance.vision public daily zips (no key, no geo-block)
├── broker.py       # python-binance spot wrapper: paginated klines, LOT_SIZE/min-notional, long-only market orders
├── executor.py     # paper/live loop reusing the backtester's score + sizing + exit rules
└── cli.py          # argparse subcommands: test-connection, download, backtest, walkforward, paper, live

tests/              # 32-test pytest suite (see below)
experiments/        # sweep.py (plateau robustness), make_fixtures.py + parity_check.js (JS/Python parity); not imported by src/
scripts/            # download_data.py, simulate_90d.py — runnable entry points that sys.path-insert src/
docs/               # STRATEGY.md (the design rationale), engine.js, static GitHub Pages site (index.html, simulator.html)
config.yaml         # all reproducible run parameters (secrets NEVER here — env vars only)
DEPLOY.md           # repo push, GitHub Pages, real-data reproduction, testnet paper trading
data/               # downloaded klines CSVs (gitignored except .gitkeep)
reports/            # generated plots (gitignored)
```

## Common commands

```bash
# Data: 90 days of 1m klines from public dumps (no API key; --source api uses REST)
python scripts/download_data.py --symbol NEARUSDT --days 90

# Fixed-config backtest (optimistic upper bound)
near-bot backtest --csv data/klines_1m.csv --trades --plot reports/backtest.png

# Walk-forward validation (the endorsed number; OOS windows only)
near-bot walkforward --csv data/klines_1m.csv --plot reports/oos.png
near-bot walkforward --csv data/klines_1m.csv --grid grid.yaml --top-k 3   # custom YAML grid

# One-shot real-data reproduction (backtest + walk-forward + USDT P&L summary)
python scripts/simulate_90d.py --symbol NEARUSDT --days 90 --capital 1000 --plot

# Paper trading on the testnet (keys from https://testnet.binance.vision)
cp .env.example .env && export $(cat .env | xargs)
near-bot test-connection
near-bot paper

# Plateau-robustness sweep (perturbs one parameter at a time; needs real data CSV)
python experiments/sweep.py --csv data/klines_1m.csv
```

`near-bot live` trades real funds and is guarded by an interactive
`I ACCEPT THE RISK` prompt — never automate around it.

## Testing

```bash
pytest -q                                # 32 tests, all offline (synthetic fixtures)
ruff check src tests                     # lint (no ruff config pinned; see note below)
python experiments/make_fixtures.py      # regenerate parity fixtures (writes /tmp/*.json)
node experiments/parity_check.js         # JS engine == Python engine (needs Node.js)
```

Testing strategy and conventions:

- **Deterministic synthetic data**: `tests/conftest.py::make_1m_klines`
  (seeded RNG) generates 1m klines with alternating trending/choppy regime
  blocks and direction-leaning taker flow, so regime gates have real structure
  to switch on. Tests aggregate it to 15m bars so warmup fits the fixture.
- **Honesty invariants are the load-bearing tests** (`test_backtest.py`):
  higher costs must never improve results; truncating the future must not
  change past trades (no look-ahead); hysteresis reduces churn; per-bar
  returns sum to equity exactly; fees land on entry/exit bars only, verified
  against hand-computed trades on hand-built 5-bar frames.
- **Causality tests** (`test_indicators.py`, `test_score.py`): mutating future
  bars must not change any past indicator/score value. Every indicator is a
  pure rolling/EWM function of past data — preserve this rigorously; a single
  leak silently invalidates every OOS number.
- Aggregation drops the incomplete trailing bar (`drop_incomplete=True`) —
  trading on an unfinished bar is a classic look-ahead bug.
- Walk-forward tests cover window discipline, embargo gaps, top-k ensemble
  behaviour, the min-trades gate, determinism, and grid validation.

CI (`.github/workflows/ci.yml`) runs on push/PR: a Python 3.10–3.12 matrix
with `ruff check src tests` + `pytest -q`, plus a separate **parity job**
(make_fixtures + node parity_check).

**Engine parity**: `docs/engine.js` is a faithful port of
`{data,indicators,score,backtest}.py`. If you change any of those Python
modules (or their defaults), you must make the equivalent change in
`docs/engine.js` and re-run the parity check — CI enforces identical trades,
prices, weights and metrics across several parameter/cost/aggregation cases.
Conversely, keep the Python engine free of pandas idioms that resist exact JS
reproduction (the check compares to 1e-9 on prices, 1e-6 on metrics).

**Ruff note**: the project has no `[tool.ruff]` config; `ruff>=0.6` is
installed unpinned. Newer ruff versions (e.g. 0.17.x) flag extra default rules
(I001 import sorting, ISC004 implicit string concatenation, SIM117, UP037,
TRY401) that the codebase written against ruff 0.6 does not satisfy. As of
this writing `ruff check src tests` reports 8 such stylistic findings while
`pytest -q` passes 32/32. Decide consciously whether to fix, ignore, or pin
the rule set before treating lint failures as blockers.

## Code style

- **Module-level docstrings are essays that explain *why*, not what** — the
  reasoning, the failure mode avoided, the reference (e.g. Moskowitz/Ooi/Pedersen
  TSMOM, Bailey–López de Prado deflated Sharpe). Match this voice: a new module
  or a non-obvious choice deserves a docstring that argues for it. Keep them
  accurate when changing behaviour — stale rationale is worse than none.
- `from __future__ import annotations` at the top of every module; type hints
  on public functions.
- Configuration is typed dataclasses (`config.py`) mirroring `config.yaml`
  sections (`strategy:`, `risk:`, `costs:`). Lookbacks are in *aggregated bars*
  (60m default): `mom_fast=36` ≈ 1.5 days, `trend_window=200` ≈ 8 days.
- Optional/heavy dependencies (`python-binance`, `matplotlib`, `yaml` fallback)
  are imported lazily inside functions so the core engine and tests run without
  them and without network.
- The trading loop is plain numpy arrays and explicit index arithmetic (see
  `backtest._run_loop`) with an exact per-bar accounting convention — every
  bar's return is written exactly once. If you touch accounting, update
  `test_per_bar_accounting_hand_computed` / `test_fee_placement_hand_computed`
  in lockstep.
- Walk-forward grids are deliberately tiny (`DEFAULT_GRID` = 12 combos); the
  project treats each added grid cell as another lottery ticket for a lucky
  in-sample winner. Do not expand grids casually.

## Security and operational considerations

- **Secrets live only in environment variables** `BINANCE_API_KEY` /
  `BINANCE_API_SECRET` (see `.env.example`); `config.Config.load()` reads them
  from the env and `to_dict()` strips them. Never put keys in `config.yaml`,
  fixtures, logs, or test data, and never commit `.env` (gitignored).
- `paper_trading: true` in `config.yaml` routes the broker to
  `https://testnet.binance.vision`; `near-bot paper/live` force this flag.
- The executor is long-only spot, sizes against actual free USDT, caps
  deployment at `risk.max_notional_frac`, rounds quantities down to Binance
  LOT_SIZE with Decimal math (float modulo breaks on steps like 1e-05), and
  checks min-notional before placing market orders.
- Downloaded klines (`data/*.csv`), generated plots (`reports/`) and parity
  fixtures (`/tmp/*.json`) are throwaway artifacts and gitignored — do not
  commit them.
- The live loop swallows per-cycle exceptions to stay alive on transient API
  errors (logs them via `logger.exception`); preserve that resilience.

## Deployment

`DEPLOY.md` is the source of truth. The three deliverables: the GitHub repo
(CI as above), the **GitHub Pages site** (repo Settings → Pages → deploy from
branch `main`, folder `/docs` — serves `index.html` + `simulator.html`, where
the simulator fetches Binance public klines in the visitor's browser with a
CSV-upload fallback), and an optional paper-trading instance on the testnet.

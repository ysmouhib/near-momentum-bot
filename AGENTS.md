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

**Trend Rider** (`docs/DESIGN.md`, the implementation contract) hardens v2
into a production single-bot system with two layers *above* the unchanged
engine:

- **Human permission switch** — `near-bot arm` / `disarm` (a `--note` is
  mandatory), 1–2 regime calls per day at most. DISARMED blocks *new entries
  only*; open positions keep their engine exits. Every call is journaled
  append-only in `state/journal.csv` and scored later (`near-bot journal report`).
- **Mechanical risk overlay** (`overlay.py`) — kill-switch ladder: −2% on the
  UTC day halts entries for the day; −5% from the equity HWM halves size
  (clears at −3%); −10% flattens (the *only* overlay-forced exit) and waits
  for a human re-arm. Every arm starts a half-size transition cooldown (3
  trades). Sizing is risk-per-trade over the chandelier stop distance, capped
  at 40% exposure. Leaving STOPPED re-anchors the full stop at the next
  equity reading (otherwise the flattened account would re-stop forever).
- **Validation battery** — `near-bot validate`: multi-symbol walk-forward
  over one fixed dated window, block-bootstrap CIs, deflated Sharpe against
  the trials ledger (`stats.TRIALS_TRIED`), N_eff (independent decisions),
  BTC-regime conditioning, and a mandatory noise-clause verdict.

**Not financial advice.** It defaults to the Binance testnet (paper trading).

## Technology stack and build

- **Python ≥ 3.10**, `src/` layout, packaged with **setuptools**
  (`pyproject.toml`). Console entry point: `near-bot = near_bot.cli:main`
  (also `python -m near_bot`).
- Core deps: `pandas>=2.0`, `numpy>=1.24`, `requests>=2.31`, `PyYAML>=6.0`.
  Optional extras: `[live]` = `python-binance` (broker wrapper, imported
  lazily so tests need no network), `[plot]` = `matplotlib` (report plots,
  also lazily imported), `[dev]` = `pytest`, `ruff==0.17.1` (pinned exactly —
  see "Lint" below).
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
├── config.py       # typed dataclass Config (YAML + env secrets); Score/Risk/Cost/Data/Overlay/ValidationParams
├── data.py         # Binance kline parsing, N-minute aggregation (drops incomplete bar), dump loading, find_gaps/window_gaps
├── indicators.py   # pure causal vectorised indicators: TSMOM, ER, Donchian, OFI z-score, ATR, vol; add_all(), warmup_bars()
├── score.py        # composite conviction score + regime gates; explain() one-liner
├── backtest.py     # event-driven loop, hysteresis, vol-target sizing, exact per-bar MTM accounting, metrics; optional overlay/armed path
├── walkforward.py  # rolling train/test windows, Sharpe selection, top-k ensemble, optional embargo; DEFAULT_GRID (12 combos, deliberately small)
├── overlay.py      # pure kill-switch state machine, transition cooldown, risk sizing; atomic JSON state persistence
├── journal.py      # append-only decision journal + accuracy report (replayed engine P&L, bootstrap/Wilson CIs, measured break-even)
├── stats.py        # block bootstrap CIs, deflated Sharpe + TRIALS_TRIED ledger, decision clustering (N_eff), Wilson CI
├── validate.py     # the validation battery behind `near-bot validate` (CIs, DSR, N_eff, BTC regime, verdict)
├── report.py       # matplotlib equity/drawdown/exposure/trade plots (optional dep)
├── dump_client.py  # data.binance.vision public daily+monthly zips (no key, no geo-block); fetch_range + manifest
├── download.py     # the one download pipeline behind both entry points: CSV + manifest, gap checks
│                   #   (allow-list), republished-file detection, --from-listing
├── broker.py       # python-binance spot wrapper: paginated klines, LOT_SIZE/min-notional, long-only market orders, equity_usdt()
├── executor.py     # paper/live loop reusing the backtester's score + sizing + exit rules; overlay, restart reconciliation
└── cli.py          # argparse subcommands: test-connection, download, backtest, walkforward, validate,
                    #   arm, disarm, journal {list,report}, status, paper, live

tests/              # 137-test pytest suite (see below)
experiments/        # sweep.py (plateau robustness), regime_sweep.py (value of regime-call accuracy, DESIGN.md §7.4),
                    #   plateau_hunt.py (pre-registered parameter search + gated holdout shot),
                    #   make_fixtures.py + parity_check.js (JS/Python parity); not imported by src/
scripts/            # download_data.py, simulate_90d.py — runnable entry points that sys.path-insert src/
docs/               # STRATEGY.md (v2 rationale), DESIGN.md (Trend Rider contract), engine.js, GitHub Pages site
config.yaml         # all reproducible run parameters (secrets NEVER here — env vars only)
DEPLOY.md           # repo push, GitHub Pages, real-data reproduction, testnet paper trading
data/               # downloaded klines CSVs + .manifest.json sidecars (CSVs gitignored)
state/              # journal.csv + overlay_state.json (gitignored — and must be BACKED UP)
reports/            # generated plots and validate.md (gitignored)
```

## Common commands

```bash
# Data: a fixed window of 1m klines from public dumps (no API key; --source api uses REST)
# writes data/NEARUSDT_1m_20230101_20260101.csv + a .manifest.json sidecar (sha256 per
# file, gaps, first/last bar); gaps > 5 min are listed, a gap > 1 day fails unless
# allow-listed in the manifest's allowed_gaps (with a reason) or --allow-gaps; a re-run
# that finds a republished file (sha256 changed) fails loudly; --from-listing starts a
# symbol listed after --start at its first bar instead of failing (recorded)
python scripts/download_data.py --symbols NEARUSDT ETHUSDT SOLUSDT BTCUSDT --start 2023-01-01 --end 2026-01-01
near-bot download --symbols NEARUSDT --start 2023-01-01 --end 2026-01-01   # same, via the CLI

# Fixed-config backtest (optimistic upper bound); --overlay adds the risk overlay
near-bot backtest --csv data/NEARUSDT_1m_20230101_20260101.csv --trades --plot reports/backtest.png

# Walk-forward validation (OOS windows only); --overlay validates the production posture
near-bot walkforward --csv data/NEARUSDT_1m_20230101_20260101.csv --plot reports/oos.png
near-bot walkforward --csv data/NEARUSDT_1m_20230101_20260101.csv --grid grid.yaml --top-k 3

# The validation battery (the endorsed numbers): every data.symbols CSV of one
# window, 90d/30d and 30d/10d variants, CIs + deflated Sharpe + N_eff + verdict
near-bot validate                                   # -> reports/validate.md + plots
near-bot validate --windows 90:30 --n-boot 2000 --no-plots

# Human permission layer (works offline; equity/price are filled in when keys are set)
near-bot arm --note "BTC reclaimed 200h MA, ER rising"   # --note is mandatory
near-bot disarm --note "chop after CPI"
near-bot status                                     # overlay state + BTC/ETH regime context
near-bot journal list
near-bot journal report --horizon-days 5            # accuracy vs measured break-even

# Legacy one-shot 90-day reproduction (rolling window — not reproducible across days)
python scripts/simulate_90d.py --symbol NEARUSDT --days 90 --capital 1000 --plot

# Paper trading on the testnet (keys from https://testnet.binance.vision);
# the overlay is forced on and a fresh state is DISARMED: arm before expecting trades
cp .env.example .env && export $(cat .env | xargs)
near-bot test-connection
near-bot paper

# Plateau-robustness sweep (perturbs one parameter at a time; needs real data CSV)
python experiments/sweep.py --csv data/NEARUSDT_1m_20230101_20260101.csv

# Synthetic-regime sweep: what is regime-call accuracy p worth to engine + overlay?
# (labels from realised walk-forward P&L; M seeded synthetic humans per p;
#  empirical break-even vs the arithmetic, 40% and 56%) -> reports/regime_sweep.md
python experiments/regime_sweep.py --csv data/NEARUSDT_1m_20230101_20260101.csv

# Pre-registered plateau hunt on 2021-2024 (spec fixed in the file; never edit the
# spec after seeing results — a new spec is a new pre-registration + ledger charge)
python experiments/plateau_hunt.py                  # -> reports/plateau_hunt.md + .csv
# the one holdout shot: ONLY with explicit human approval
python experiments/plateau_hunt.py --holdout --center theta_in=... trail_atr=... er_min=...
```

`near-bot live` trades real funds and is guarded by an interactive
`I ACCEPT THE RISK` prompt — never automate around it.

## Testing

```bash
pytest -q                                # 137 tests, all offline (synthetic fixtures)
ruff check src tests                     # lint — must be clean (ruff pinned to 0.17.1)
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
- **Overlay tests** (`test_overlay.py`, `test_backtest_overlay.py`): every
  kill-switch transition incl. tier hysteresis, re-arm after a full stop
  (must stick, tier × cooldown = 0.25), overlay sizing and halts/flattens
  pinned to hand arithmetic on hand-built frames, `armed` edges acting as
  human arm/disarm, and the **overlay-off identity** (`run(frame, cfg)` is
  byte-identical to `run(..., overlay=None)` — this protects JS parity).
- **Executor tests** (`test_executor.py`, FakeBroker, offline): permission
  gating, fail-closed overlay errors, full-stop flatten, restart
  reconciliation (incl. trailing-stop peak and bar count surviving a restart).
- **Journal / stats / validate tests**: append-only roundtrip, `--note`
  enforcement, truncated forward windows marked open, full-stop losses in the
  measured break-even, CIs that cannot collapse on short records, bootstrap
  determinism (the vectorised resampler is pinned bit-for-bit to the scalar
  reference), deflated Sharpe, decision clustering, BTC-regime causality, and
  the `near-bot validate` CLI end to end.
- **Regime-sweep tests** (`test_regime_sweep.py`, loads the experiment by
  path): on hand-built windows the Monte Carlo break-even must land on the
  hand-computed one, and the replay cases must honour the overlay contract
  (continuing armed = no cooldown, fresh arm = cooldown, disarmed = flat).

CI (`.github/workflows/ci.yml`) runs on push/PR: a Python 3.10–3.12 matrix
with `ruff check src tests` + `pytest -q`, plus a separate **parity job**
(make_fixtures + node parity_check). Keep code 3.10-compatible (e.g.
`dt.timezone.utc`, not the 3.11+ `dt.UTC`).

**Engine parity**: `docs/engine.js` is a faithful port of
`{data,indicators,score,backtest}.py`. If you change any of those Python
modules (or their defaults), you must make the equivalent change in
`docs/engine.js` and re-run the parity check — CI enforces identical trades,
prices, weights and metrics across several parameter/cost/aggregation cases.
Conversely, keep the Python engine free of pandas idioms that resist exact JS
reproduction (the check compares to 1e-9 on prices, 1e-6 on metrics). The
overlay/`armed` path in `backtest.py` is **off by default and not ported to
JS** (deferred, DESIGN.md §11): parity fixtures never pass an overlay, so
they stay valid; never let overlay logic leak into the default path.

**Lint**: there is no `[tool.ruff]` config — the project uses ruff's default
rule set, and ruff is pinned exactly (`ruff==0.17.1` in pyproject's `[dev]`
extra and `requirements-dev.txt`; CI asserts the version) because the default
rule set grows between releases and an unpinned linter turns CI red without a
line of code changing. `ruff check src tests` must stay clean. Bump the pin
deliberately, fixing any new findings in the same change. Implicit string
concatenation inside a list/tuple must be parenthesised (ISC004).

## Code style

- **Module-level docstrings are essays that explain *why*, not what** — the
  reasoning, the failure mode avoided, the reference (e.g. Moskowitz/Ooi/Pedersen
  TSMOM, Bailey–López de Prado deflated Sharpe). Match this voice: a new module
  or a non-obvious choice deserves a docstring that argues for it. Keep them
  accurate when changing behaviour — stale rationale is worse than none.
- `from __future__ import annotations` at the top of every module; type hints
  on public functions.
- Configuration is typed dataclasses (`config.py`) mirroring `config.yaml`
  sections (`strategy:`, `risk:`, `costs:`, `data:`, `overlay:`,
  `validation:`). Lookbacks are in *aggregated bars* (60m default):
  `mom_fast=36` ≈ 1.5 days, `trend_window=200` ≈ 8 days.
- Overlay thresholds are **a-priori risk policy**, never walk-forward
  optimised; every configuration evaluated (grid cell, window variant,
  overlay threshold) is a trial — bump `stats.TRIALS_TRIED` when you try one.
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
- **`state/` is gitignored but is not throwaway**: `journal.csv` is the
  measurement instrument for the human's accuracy (append-only — no edit
  command; fix mistakes by appending a `correction` event) and
  `overlay_state.json` holds the HWM, kill-switch tier, cooldown and the open
  position. Back it up; losing the journal resets the track record to zero.
  State writes are atomic (tmp + rename).
- The live loop swallows per-cycle exceptions to stay alive on transient API
  errors (logs them via `logger.exception`); preserve that resilience. Overlay
  evaluation failures **fail closed** (entries blocked for that cycle), and a
  state/exchange mismatch at startup blocks entries until a human resolves it.

## Deployment

`DEPLOY.md` is the source of truth. The three deliverables: the GitHub repo
(CI as above), the **GitHub Pages site** (repo Settings → Pages → deploy from
branch `main`, folder `/docs` — serves `index.html` + `simulator.html`, where
the simulator fetches Binance public klines in the visitor's browser with a
CSV-upload fallback), and an optional paper-trading instance on the testnet.



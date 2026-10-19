# DESIGN.md — "Trend Rider": v2 engine + human permission layer + risk overlay

Status: **implemented through step 5 except the regime sweep** (progress per
step in §11; the multi-year validation run itself has not been done yet).
This document is the implementation contract for hardening the v2 research framework into a
production single-bot system. It is written to be argued with, in the same
spirit as `docs/STRATEGY.md`: every claim here is checkable against the repo,
and every number the system will ever report must survive the validation
protocol in §7.

## 0. What this system is, and what it is not

Trend Rider = the v2 score-ensemble engine (unchanged) + two layers on top:

1. **A human permission switch at the regime level.** The human answers one
   question, 1–2 times per day at most (often 1–2 times per week): *is a
   directional move plausible in the coming days?* Answer = bot ARMED or
   DISARMED. The human **never** touches trade-level decisions: entries are
   owned by the conviction score (`score.py`), exits by the engine's rules
   (score fade / chandelier trailing stop / time stop, `backtest.py:191-198`).
2. **A mechanical risk overlay** (kill-switch ladder, transition cooldown,
   per-trade risk sizing, exposure cap) that sits *above* the engine and can
   only reduce or block exposure — with exactly one exception, the full stop,
   which flattens (§4.2).

Spot market, long/flat only, one symbol in production (NEARUSDT). The other
symbols (ETH, SOL, BTC) exist for validation and context, not for a
multi-bot deployment.

**The arithmetic that justifies the human layer.** With per-trade expectancy
≈ +0.9% when the regime call is right and ≈ −0.6% when it is wrong (taker
costs included), the break-even regime accuracy is

```
p* = L / (W + L) = 0.6 / (0.9 + 0.6) = 0.40
```

Note: the project brief states ~56%. With the stated W/L pair the figure is
40%; 56% would require roughly W/L ≈ 0.79 (e.g. +0.9% vs −1.14%). This
discrepancy does not change the design, because the design's core rule is
that **both** the accuracy and the break-even are *measured* from the journal
and from realised trade outcomes — never asserted (§6.3). If measured W/L
implies p* = 40% and the measured human accuracy is 60–80%, the layer pays
for itself; if measured accuracy straddles p*, we say so and the layer comes
off.

**Armed-and-flat is a profit state.** v2 was invested ~30% of the time and
that is a feature: the no-trade band and the gates are the cost-awareness.
The overlay must never pressure the system to trade (no "use it or lose it"
metrics anywhere in reporting).

## 1. Invariants carried over (non-negotiable)

These come from v2 and bind every new line of code:

- **What you backtest is what you run.** Entries, exits and sizing live in
  `score.compute()` + `backtest.py`; the executor reuses them. The overlay
  must therefore exist *inside* the backtester too (§5, `backtest.py`), or
  live behaviour cannot be simulated.
- **Causality.** Signals on closed bars only; fills at next bar's open;
  incomplete aggregated bar dropped. The new BTC exogenous data (§3.4) is
  subject to the same causality tests if it ever touches a signal.
- **Exact per-bar accounting.** Every bar's return written exactly once;
  fees on fill bars only (`backtest._run_loop` docstring). Overlay-induced
  blocks and forced flattens reuse the same conventions, and new
  hand-computed tests pin them (§8).
- **Costs everywhere, costs can only hurt.** 0.24% round trip by default; any
  overlay result that *improves* when costs rise is a bug report, not a
  feature.
- **Engine parity with `docs/engine.js`.** The JS port mirrors
  `{data,indicators,score,backtest}.py`. The overlay is implemented as an
  *optional* layer that is **off by default in backtests**, so the parity
  fixtures (which exercise default paths) remain bit-identical and CI keeps
  passing. The browser simulator documents that it demos the *signal
  engine*, not the production risk overlay (§9, deferred work).
- **Grids stay tiny; every configuration tried is a trial.** The overlay's
  thresholds are fixed a priori risk policy, not tuned — and they are counted
  in the deflated-Sharpe trials ledger regardless (§7.3).

## 2. Repository-level changes at a glance

New files:

```
src/near_bot/overlay.py     # kill-switch state machine + overlay sizing (pure, testable)
src/near_bot/journal.py     # append-only journal CSV + accuracy reporting + bootstrap
src/near_bot/stats.py       # block bootstrap CIs, deflated Sharpe, independent-decision clustering
state/                      # journal.csv + overlay_state.json (gitignored, backed up — §6.1)
experiments/regime_sweep.py # synthetic human-accuracy sweep (validation §7.4)
docs/DESIGN.md              # this file
```

Modified files (details in §8): `scripts/download_data.py`,
`src/near_bot/dump_client.py`, `src/near_bot/config.py`,
`src/near_bot/backtest.py`, `src/near_bot/walkforward.py`,
`src/near_bot/executor.py`, `src/near_bot/broker.py`,
`src/near_bot/cli.py`, `tests/*`, `.gitignore`, `AGENTS.md`, `README.md`,
`DEPLOY.md`. **Not touched:** `indicators.py`, `score.py`,
`docs/engine.js`, `report.py` (except optional overlay shading, later).

## 3. Phase A — data layer

### 3.1 Fixed dated windows, not rolling windows

Today `scripts/download_data.py` takes `--days N` and `dump_client.fetch_daily`
walks back from `date.today()` — every run downloads a *different* dataset,
so "the same backtest" is not reproducible across days. Phase A replaces this
with fixed windows:

```
python scripts/download_data.py --symbols NEARUSDT ETHUSDT SOLUSDT BTCUSDT \
    --start 2023-01-01 --end 2026-01-01          # end exclusive
```

- `--days` is removed (or hard-deprecated with a warning); `--start` is
  required, `--end` defaults to the latest *complete* UTC day.
- Output: one CSV per symbol, `data/{SYMBOL}_1m_{START}_{END}.csv`, plus a
  manifest (§3.3). Same command → same bytes → same bars → same results.
- `near-bot download` gains the same flags so the CLI and the script stay
  equivalent entry points.

### 3.2 Monthly dumps for multi-year pulls

`dump_client.py` currently only knows daily zips
(`data.binance.vision/data/spot/daily/klines/...`). Multi-year 1m data as
daily zips is ~1000+ HTTP requests per symbol — slow and fragile. Add:

- `MONTHLY_BASE = ".../data/spot/monthly/klines/{symbol}/1m/{fname}"` and
  `fetch_monthly(symbol, year, month)`.
- `fetch_range(symbol, start, end)`: monthly zips for every complete month
  in `[start, end)`, daily zips only for a partial leading/trailing month.
  Roughly 36 requests instead of ~1100 for a 3-year pull.
- The existing timestamp-unit sniffing (`_to_datetime`, ms vs µs) already
  handles the dump format change; monthly files get the same treatment.
- Optional but cheap: fetch the `.CHECKSUM` sidecar each dump publishes and
  verify SHA-256 before parsing. A corrupt zip currently fails at
  `zipfile.ZipFile`, which is acceptable; a corrupt *CSV inside a valid zip*
  is not — checksums close that hole.

Scale notes: ~525,600 1m rows per symbol per year → ~1.6M rows for a 3-year
window (~80–100 MB CSV per symbol). Pandas handles this fine; the engine only
ever sees the aggregated 60m bars (~26k per 3 years). The 1m CSVs remain the
source of truth and stay gitignored throwaways — they are re-downloadable
from the manifest.

### 3.3 Manifest and gap detection (fail loud, not silent)

`fetch_daily` today does `if resp.status_code == 404: continue` — on a 90-day
pull that silently swallows a missing day, and on a 3-year pull it *will*
happen (listing gaps, exchange incidents, late-published corrections). For
multi-year validation this is a data-integrity hazard, so:

- `fetch_range` returns `(df, manifest)`; the manifest records every file
  requested, rows parsed, and SHA-256, and is written next to the CSV as
  `data/{SYMBOL}_1m_{START}_{END}.manifest.json`.
- New helper `data.find_gaps(df, expected_freq="1min")` compares actual vs
  expected minute coverage and reports every gap > some tolerance (default:
  any gap > 5 minutes is listed; > 1 day fails the download unless
  `--allow-gaps`). Known exchange-maintenance windows can be allow-listed in
  the manifest explicitly — silence is what we are removing, not gaps.
- Reproducibility: re-running the same command re-checks the manifest; if
  Binance republishes a corrected file, the checksum mismatch is reported,
  not ignored.

### 3.4 BTC as exogenous context — decision support, not a signal (yet)

BTC 1m data serves three explicitly non-engine purposes in this phase:

1. **Human decision support.** `near-bot status` (§4.5) prints BTC/ETH
   context — price vs 200-bar SMA, Kaufman ER, realised vol — because the
   human's daily arm/disarm call is mostly a call on *market* regime, and
   BTC is the cleanest proxy for it.
2. **Validation conditioning.** The validation battery (§7) reports engine
   OOS results conditioned on BTC regime (trending up / chop / down), so we
   learn whether the NEAR edge is just the BTC beta wearing a costume.
3. **A frozen option.** A BTC-derived gate inside `score.compute()` is a
   legitimate future experiment, but it is a *score change*: it requires
   causality tests, a `docs/engine.js` port, parity-fixture regeneration,
   and an entry in the trials ledger. None of that is in this phase.

## 4. Phase B — the risk overlay

### 4.1 Contracts and vocabulary

The overlay reasons about *permission* and *size multipliers*, never about
individual trade direction. Precisely:

- **ARMED / DISARMED** — human-set permission. DISARMED blocks *new entries
  only*. An open position continues to be managed by the engine's exits
  (score fade, trailing stop, time stop). The human does not override trades,
  and disarming is not a sell button.
- **Size multiplier** `m ∈ (0, 1]` applied to every *new* entry's weight.
  Two independent sources multiply: the kill-switch tier and the
  post-arm transition cooldown. `m = m_tier × m_cooldown`, floored at 0.25;
  if the resulting weight falls below `min_weight`, the entry is skipped
  (dust rule unchanged).
- **Equity** — live: free USDT + base balance × current price, marked to
  market each cycle. Backtest: the running equity from the exact per-bar
  accounting (including open-position MTM — same convention as `_metrics`).
- **High-water mark (HWM)** — max equity ever observed, persisted.
- **Day** — UTC calendar day. "Day-start equity" is the equity at the first
  observed bar/cycle of the UTC day.

### 4.2 Kill-switch ladder — state machine

States and what they mean:

```
            ┌──────────────────────────────────────────────────┐
            │                                                  │
   DISARMED ⇄ ARMED ──(daily loss ≤ −2% vs day-start)──► HALTED_TODAY
     ▲          │                                              │ next UTC day
     │          │(dd from HWM ≤ −5%)                           ▼
     │          ▼                                          ARMED (entries
     │      HALF_SIZE tier (m_tier = 0.5;                  resume)
     │        clears when dd recovers above −3%)
     │          │
     │          │(dd from HWM ≤ −10%)
     │          ▼
     └──── STOPPED ── flatten any open position at market/next open,
       human re-arm    journal auto event, no entries until `near-bot arm`
```

Transition rules, each one deliberate:

- **Daily halt (−2%).** Blocks new entries until the next UTC day. The open
  position (if any) is *not* touched — its exits belong to the engine. A
  −2% day is information ("the regime call may be wrong today"), not an
  emergency.
- **Half size (−5% from HWM).** `m_tier = 0.5` on new entries. Clears when
  the drawdown recovers above **−3%** — a hysteresis band, because a tier
  that toggles on every tick around −5.00% is a churn machine wearing a
  risk-management hat.
- **Full stop (−10% from HWM).** The one and only overlay-forced exit:
  flatten at the next bar's open (backtest) / market sell (live), journal an
  `auto_full_stop` event, block everything until the human re-arms. This is
  the overlay's emergency brake; it exists because a −10% hole with a
  trend-follower that is *supposed* to be gated means either the regime call
  was catastrophically wrong or the engine's assumptions broke. Both are
  stop-and-think conditions, by definition not automatable.
- **Re-arm after STOPPED** goes through the transition cooldown (§4.3) —
  the two mechanisms compose on purpose.
- **Risk-of-ruin context:** at +0.05% daily edge and 1% daily equity vol, a
  −20% drawdown has ~13% probability; at zero edge it is a certainty. The
  −10% full stop sits far inside −20%, so a stopped-out system has lost at
  most half its ruin budget and the human is forced to re-underwrite the
  regime thesis before more capital is exposed.

The state machine lives in `overlay.py` as a pure dataclass + transition
function (no I/O, no broker), persisted by callers to
`state/overlay_state.json`. Pure → unit-testable → the same object drives
the backtest overlay and the executor. This is the §1 invariant applied to
the overlay itself.

### 4.3 Transition cooldown

Regime-call errors cluster at transitions: the moment the human flips the
switch is the moment they are most likely early or wrong. So after **every**
human `arm` (including re-arm after STOPPED), the first
`cooldown_trades_after_arm` (default 3) entries are taken at
`m_cooldown = 0.5`. The counter decrements per *closed* trade, persists in
the state file, and is journaled when it exhausts (`cooldown_done`).

Composed example: re-arm after a full stop, still −6% below HWM →
`m = 0.5 (tier) × 0.5 (cooldown) = 0.25` for the first 3 trades. That is the
system working as designed, not a malfunction.

### 4.4 Per-trade risk sizing and exposure caps

v2 sizes purely by volatility target (`backtest.position_weight`). The
overlay adds a risk-per-trade budget and caps, computed *at entry* from the
same stop the engine will actually trail (chandelier = `trail_atr × ATR`):

```
stop_dist_frac = trail_atr * ATR(entry) / entry_price
w_risk         = per_trade_risk_frac / stop_dist_frac      # R / stop distance
w_vol          = position_weight(vol_bar, ...)             # existing v2 sizing
w              = min(w_vol, w_risk, max_exposure_frac - open_exposure,
                     hard_max_weight) * m_tier * m_cooldown
entry skipped if w < min_weight
```

- `per_trade_risk_frac` (R) ∈ [0.5%, 0.75%] of current equity, default
  0.6%. Using *current* equity means losses automatically shrink size — the
  overlay's compounding works against ruin in both directions.
- **Total invested exposure cap: 40% of equity** (quarter-Kelly for the
  assumed edge/odds profile; the single-position bot means this is simply
  the per-entry cap in production, but the formula is written against
  `open_exposure` so multi-symbol validation exercises the same code).
- **Hard cap 100%** — spot, no leverage; this cap is structural and not
  configurable away, only down.

Sanity example: ATR = 1.0% of price, `trail_atr = 3` → stop distance 3%;
R = 0.6% → `w_risk = 0.20`. In a quiet market the vol target might allow
w = 1.0, so the exposure cap binds at 0.40 and the risk budget at 0.20 →
w = 0.20. In a wild market ATR = 4% → stop 12% → w = 0.05, right at the
dust floor. Both are the intended behaviour: *risk per trade is constant;
notional is the residual.*

### 4.5 Executor integration and restart reconciliation

The executor currently keeps its position **in memory only**
(`executor.py:57`): a restart loses it, which in a system with kill switches
and journals is unacceptable. Changes:

- On startup: load `state/overlay_state.json`; fetch balances from the
  broker; reconcile. State says long + exchange shows base balance →
  reconstruct `Position` from persisted fields (qty, entry_price, peak_close,
  entry_bar_time — all persisted at fill time). State flat + exchange shows
  non-dust base → log loudly and refuse entries until the human resolves it
  (manual exchange trades break every assumption; detect, don't absorb).
- Each cycle: update equity, HWM, day-start equity (on first cycle after
  UTC midnight), evaluate the kill switch *before* entry logic. Overlay
  events are journaled as `auto_*` events with machine-generated notes.
- DISARMED → skip `_maybe_enter` entirely; `_manage_position` runs
  unchanged. STOPPED → one flatten sell, then wait for `arm`.
- New `near-bot status` command: prints armed state, multipliers, cooldown
  counter, equity / day-start equity / HWM / current drawdown tier, plus the
  BTC/ETH context block (§3.4). This is the human's instrument panel for the
  1–2 daily decisions — it is decision *support*, not a signal.

`broker.py` gains one helper (`equity_usdt() = free USDT + base × price`);
no structural change. The per-cycle `try/except` resilience
(`executor.py:172-174`) is preserved; overlay evaluation failures fail
*closed* (treat as DISARMED for that cycle, log exception) — a risk overlay
that crashes open is worse than none.

## 5. Overlay inside the backtester (the load-bearing change)

`backtest.run` gains optional parameters:

```
run(frame, cfg, score=None, overlay: OverlayParams | None = None,
    armed: pd.Series | None = None)
```

- `overlay=None` (default) → the loop is **byte-identical** to today. This
  is what keeps `experiments/parity_check.js` green: parity fixtures never
  pass an overlay.
- `armed` — boolean per-bar series aligned to the frame; `False` blocks
  entries exactly as DISARMED does live. This is also the injection point
  for the synthetic-regime validation (§7.4): simulated human schedules are
  just `armed` series.
- With an overlay present, `_run_loop` additionally: computes `w` per §4.4
  (the ATR at the signal bar and the entry fill price give the stop
  distance); tracks equity MTM, day-start equity and HWM from the existing
  per-bar returns; applies the tier/cooldown multipliers; blocks entries on
  daily-halt and disarmed bars; flattens at next open on full stop with the
  standard exit-bar fee convention (`ret[exit_bar] += w*(fill/prev_close - 1) - w*fee`).
- `Trade` gains optional fields (`m_tier`, `m_cooldown`, `blocked_entries`
  counter on the result) so validation can attribute behaviour; defaults
  keep the dataclass backward-compatible.
- Walk-forward passes the overlay through unchanged (`_with_strategy` copy
  pattern already exists); selection remains Sharpe-based on the *engine's*
  IS returns with the overlay active, so the OOS number is the number of
  the system we would actually run.

## 6. The journal: schema, storage, reporting

### 6.1 Storage discipline

`state/journal.csv` is the **measurement instrument for the human's
accuracy** — it is the dataset the 60–80% claim is tested against, so its
integrity matters more than its convenience:

- Append-only. There is no edit command, by design. A fat-fingered note is
  corrected by appending a `correction` event that references the original,
  never by rewriting history.
- `state/` is gitignored (operational privacy, same precedent as `.env`)
  and **must be backed up** — DEPLOY.md will say so; losing the journal
  resets the human's track record to zero and the measurement restarts.
- Every human decision is journaled — including redundant ones (`arm` while
  already armed records a no-op event). The journal measures the human, so
  it records what the human did, not what changed.

### 6.2 Schema

One row per event, CSV (human-inspectable, append-friendly):

| column | content |
|---|---|
| `ts_utc` | ISO-8601 UTC timestamp of the decision |
| `event` | `arm` · `disarm` · `auto_halt_day` · `auto_half_on` · `auto_half_off` · `auto_full_stop` · `auto_flatten` · `cooldown_done` · `correction` |
| `state_after` | `armed` / `disarmed` / `stopped` (post-event) |
| `note` | **mandatory non-empty for `arm`/`disarm`** (the human's reasoning, in their words); machine-generated for `auto_*` |
| `equity` | equity at decision time (MTM, USDT) |
| `price` | mark price of the traded symbol |
| `hwm` | high-water mark at decision time |
| `context` | free-form key=value blob (score, ER, BTC-vs-MA …) — cheap now, gold at review time |

CLI: `near-bot arm --note "..."` / `near-bot disarm --note "..."` —
`--note` is `required=True`; an arm/disarm without a stated reason is
exactly the behaviour the journal exists to catch, so the tool refuses it.

### 6.3 `near-bot journal report` — measuring the human

For each `arm`/`disarm` call, score it against the subsequent N-day outcome
(`--horizon-days`, default 5), per call type:

- **Primary outcome measure:** the *strategy's* net P&L over the forward
  window, obtained by replaying the engine (+overlay) over that slice —
  because the human's job is to permission *this engine*, so the honest
  question is "did the engine make money while it was armed / stay out of
  trouble while disarmed". An `arm` is correct if forward strategy P&L > 0;
  a `disarm` is correct if forward strategy P&L ≤ 0.
- **Secondary measure:** forward buy-and-hold return over the same window
  (market-based scoring — robust to "engine flat anyway" windows, and it
  prices missed trends: a `disarm` before a +15% week is wrong even if the
  engine would have been gated off).
- Report per call type and overall: accuracy, counts, and **bootstrap CIs**
  (10k resamples over calls, percentile method; plus a cluster-robust
  variant that resamples *streaks* of consecutive same-state days, because
  consecutive calls are not independent).
- **Measured break-even:** from realised trade outcomes, `p̂* = L̂/(Ŵ+L̂)`,
  printed next to the theoretical values (40% from the stated W/L; the
  brief's 56% shown for reference). Verdict line is mandatory and blunt:
  `measured accuracy 64% [51%–76%] vs break-even 42% → distinguishable` /
  `→ NOT distinguishable at this sample size (need ~n calls)`.
- Also reported: **missed-move cost** (sum of forward market returns over
  disarmed windows — the price of caution) and armed-window exposure, so
  "armed-and-flat" reads as the success it is.

## 7. Validation plan

Everything v2 endorses stays: walk-forward OOS only, top-3 ensemble, per-bar
mark-to-market. Added:

### 7.1 Multi-year, multi-symbol

- Fixed windows from Phase A (default 3 years, e.g. 2023-01-01 → 2026-01-01).
- Window sizes scale up: default `train 90d / test 30d` (2160/720 bars at
  60m) for multi-year runs — 30d/10d stays available via flags; both are
  run and both are reported, because window choice is itself a researcher
  degree of freedom and honesty means showing it.
- Symbols: NEAR (production), ETH & SOL (held-out generalisation, as in
  v2), BTC (context + optionally traded as a fourth evidence stream,
  clearly separated). A `near-bot validate` command runs the battery per
  symbol over identical windows and writes `reports/validate.md` + plots.

### 7.2 Bootstrap CIs on OOS Sharpe and expectancy

`stats.py`: **stationary/block bootstrap** (block length 168 bars ≈ 1 week,
configurable) over the stitched OOS per-bar returns → 95% CIs for Sharpe,
annualised return, max drawdown. For expectancy, the resampling unit is the
**independent decision cluster** (§7.5), never the individual trade or the
ensemble leg. Seeded RNG → reproducible CIs.

### 7.3 Deflated Sharpe ratio (charge for every configuration tried)

`stats.deflated_sharpe(sr, T, n_trials, skew, kurt)` implements
Bailey–López de Prado: the benchmark SR* is the expected maximum Sharpe
over N independent trials under the null
(`SR* ≈ √V̂ · [(1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e))]`), and DSR is the
probabilistic Sharpe ratio against that benchmark using the OOS returns'
skew/kurtosis. **N comes from a trials ledger** — an explicit constant in
`stats.py` with a comment block enumerating what has been tried (v1's
grids, v2's 12-cell DEFAULT_GRID, the plateau sweep's ~24 perturbations,
window-size variants, overlay threshold choices, and anything tried during
this project), updated whenever a new configuration is evaluated. The
ledger is deliberately conservative (overcounting N lowers DSR). If DSR ≤ 0
after charging for all of it, the report says the edge is not established.
Full stop.

### 7.4 Synthetic-regime sensitivity sweep (does the human layer pay?)

`experiments/regime_sweep.py`:

1. Run the real walk-forward engine over the multi-year NEAR window;
  label each OOS test window by realised engine P&L (good/bad). These
  labels are forward-looking by construction — legitimate here because the
  sweep never claims to predict; it measures plumbing and value-at-accuracy.
2. For each accuracy p ∈ {0.5, 0.6, 0.7, 0.8}, generate M = 200 seeded
   synthetic humans: before each window, arm with probability p if the
   window is good, disarm with probability p if bad. Each synthetic human =
   one `armed` boolean series injected into the backtest overlay (§5).
3. Run the full system (engine + overlay) under every schedule; report
   expectancy / total return / max DD vs p, with dispersion across the M
   schedules.
4. Fit the empirical break-even accuracy and compare against both the
   theoretical p* = 40% and the brief's 56%. Agreement validates the
   overlay plumbing end-to-end; disagreement is a bug in the plumbing or
   the arithmetic, and either way it gets found here, not in production.

Caveat printed in the report: synthetic labels give the human
clairvoyant-grade *information basis* — the sweep maps the value of
accuracy, it does not certify that a real human achieves p = 0.7. Only the
journal (§6.3) measures that.

### 7.5 Independent decisions, not ensemble legs

`stats.decision_clusters(trades)`: two trades belong to the same decision
if their [entry, exit] intervals overlap in time (across top-3 ensemble legs
and across symbols). Report **N_eff** next to every trade count — the
current "13 independent decisions" problem is the whole reason for this
section. Expectancy CIs bootstrap over clusters. Target for the multi-year
multi-symbol run: N_eff in the hundreds; if it is still in the dozens, the
report says the result remains statistically thin, explicitly.

### 7.6 The noise clause (standing rule)

Every validation output — validate report, journal report, regime sweep —
ends with a mandatory verdict section. If any headline number's CI includes
zero, or DSR ≤ 0, or N_eff is too small for the CI to exclude zero, the
report states in plain words: *"this result is not statistically
distinguishable from noise at this sample size."* This is not a disclaimer
template; it is the deliverable when true, same as v2's README does today.

## 8. Module-by-module change list

| module | change |
|---|---|
| `scripts/download_data.py` | `--symbols`, `--start`/`--end` fixed windows; monthly-dump path; manifest write; gap check; `--days` deprecated |
| `dump_client.py` | `MONTHLY_BASE`, `fetch_monthly`, `fetch_range` (monthly + partial daily), optional `.CHECKSUM` verification, manifest collection |
| `data.py` | add `find_gaps()`; loader for the dated-window CSV naming. Additive only — parity unaffected |
| `config.py` | new `OverlayParams` dataclass (§9); `data:` section (symbols, start, end, dir); `validation:` section; YAML mirroring; secrets handling unchanged |
| `indicators.py` | **untouched** |
| `score.py` | **untouched** |
| `backtest.py` | optional `overlay`/`armed` parameters (§5); overlay branches in `_run_loop`; new `Trade` fields (defaulted); `metrics` gains `n_blocked_entries`, kill-switch event counts. Default path byte-identical |
| `overlay.py` (new) | pure state machine (§4.2), sizing composition (§4.4), cooldown counter; JSON (de)serialisation; no I/O, no broker, no pandas-hot-path idioms |
| `journal.py` (new) | append/load journal; event enum; report computation (§6.3) using `stats.py` |
| `stats.py` (new) | block bootstrap CIs, deflated Sharpe + trials ledger, decision clustering, serial-correlation-aware accuracy CIs |
| `walkforward.py` | pass overlay through; expose per-window OOS returns for bootstrap; wire N_eff into summary |
| `executor.py` | overlay evaluation per cycle; persisted position + restart reconciliation (§4.5); journal auto-events; fail-closed on overlay errors |
| `broker.py` | add `equity_usdt()`; unchanged otherwise |
| `cli.py` | new: `arm`, `disarm`, `journal report`, `status`, `validate`; `download` flags; all overlay commands work against testnet paper mode by default |
| `experiments/regime_sweep.py` (new) | §7.4 |
| `tests/` | §8.1 below |
| `.gitignore` | add `state/` |
| `docs/engine.js` | **untouched** — overlay is off the default path; simulator page gets a one-line note that it demos the signal engine without the production risk overlay |
| `AGENTS.md`, `README.md`, `DEPLOY.md` | updated at implementation: new commands, `state/` backup rule, overlay semantics, parity stance |

### 8.1 Tests (the overlay earns its place the same way v2 did)

- **State machine:** every transition in §4.2, including hysteresis
  (tier does not flap at −4.99%/−5.01%), cooldown countdown, re-arm
  composition, idempotent arm/disarm.
- **Hand-computed overlay accounting**, mirroring
  `test_per_bar_accounting_hand_computed`: a 5-bar frame where the overlay
  blocks an entry, halves a size, and force-flattens — returns and fees
  pinned to hand arithmetic; fee on the forced flatten lands on the exit
  bar, exactly once.
- **Armed gating:** an `armed=False` span produces zero entries and leaves
  exits alone; `armed` series shifted one bar changes past nothing
  (causality of the injection path).
- **Overlay-off identity:** `run(frame, cfg)` == `run(frame, cfg, overlay=None)`
  bit-for-bit (this is the test that protects engine parity).
- **Journal:** roundtrip append/load; `--note` enforcement; report math on
  a hand-built journal; bootstrap determinism under fixed seed.
- **Stats:** bootstrap CI contains the true value on synthetic series with
  known Sharpe; deflated Sharpe decreases as trials N grows; clustering
  merges overlapping legs into one decision.
- Existing 32 tests must pass unmodified — including all causality and
  honesty invariants.

## 9. Config schema (additions to `config.yaml`)

```yaml
symbol: NEARUSDT            # production traded symbol (single bot)

data:
  symbols: [NEARUSDT, ETHUSDT, SOLUSDT, BTCUSDT]   # download + validation universe
  start: "2023-01-01"       # fixed window, inclusive
  end: "2026-01-01"         # exclusive; default at runtime = latest complete UTC day
  dir: data

overlay:
  enabled: true             # paper/live force this on; backtest/walkforward
                            # default OFF (parity) unless --overlay is passed
  per_trade_risk_frac: 0.006    # R per trade, in [0.005, 0.0075]
  max_exposure_frac: 0.40       # quarter-Kelly cap on invested notional
  hard_max_weight: 1.00         # structural (spot, no leverage); only lower allowed
  daily_loss_halt_frac: -0.02   # vs UTC day-start equity → halt entries for the day
  hwm_half_size_frac: -0.05     # drawdown from HWM → m_tier = 0.5
  hwm_half_clear_frac: -0.03    # recovery hysteresis to clear the half-size tier
  hwm_full_stop_frac: -0.10     # flatten + require human re-arm
  transition_size_mult: 0.5
  cooldown_trades_after_arm: 3
  min_multiplier: 0.25
  journal_path: state/journal.csv
  state_path: state/overlay_state.json

validation:
  train_days: 90
  test_days: 30
  top_k: 3
  bootstrap_reps: 10000
  bootstrap_block_bars: 168
  journal_horizon_days: 5
  synthetic_accuracies: [0.5, 0.6, 0.7, 0.8]
  synthetic_schedules: 200
```

The overlay thresholds are **risk policy chosen a priori**, not fitted
parameters: they are never walk-forward optimised, and each value tried
during development still goes into the trials ledger.

## 10. What could still kill this system in production

Ordered roughly by "most likely to be the actual obituary":

1. **The edge was never there.** Multi-year DSR ≤ 0 after charging for all
   trials, or OOS CIs include zero across symbols. The noise clause (§7.6)
   fires; the honest outcome is not shipping. This design cannot fix a
   signal problem and does not pretend to.
2. **The human underperforms the break-even, or it takes years to find
   out.** At 1–2 journalable calls per week, n ≈ 50–100 per year; the binomial
   CI around 65% at n=100 is roughly ±10 points — wide enough to straddle
   any plausible break-even for a long time. Mitigation is structural only:
   the journal starts measuring on the testnet *now*, and the synthetic
   sweep (§7.4) tells us in advance how much accuracy is worth so we know
   what precision we need.
3. **Regime-call timing.** Trends start at 04:00 local; the human sleeps.
   The journal's missed-move metric quantifies this cost; if it dominates
   the avoided-loss benefit, the layer comes off. No software fix exists —
   it is the accepted shape of the system.
4. **Data pipeline rot.** Silent 404 days (today's `continue`), ms/µs dump
   format changes, republished corrections, delisted pairs. Mitigated by
   manifests + gap detection + checksums (§3.3), but a multi-year gap in
   NEAR's early history may simply exist — then the window moves and the
   report says why.
5. **State corruption / crash mid-trade.** Power loss between fill and
   state write; hand-edited JSON. Mitigations: atomic state writes
   (write-tmp-then-rename), startup reconciliation against exchange
   balances (§4.5), and fail-closed behaviour. Residual risk: a manually
   placed exchange order breaks reconciliation — detected and refused, not
   absorbed.
6. **Overlay self-harm.** A flatten at the local bottom (the −10% stop is
   not clairvoyant), tier-boundary whipsaw (hysteresis mitigates), or
   half-size orders falling below Binance min-notional on a small account
   (skipped with a logged reason — the dust rule already exists).
7. **Overlay parameter creep.** Thresholds "improved" after seeing the
   data = a new overfitting channel wearing a risk-management costume.
   Defence is procedural: a priori values, trials ledger, and the plateau
   habit from v2 applied to the overlay too.
8. **Cost drift.** Fee schedule changes, slippage in stressed books far
   above the flat 0.02% assumption. Quarterly re-check of realised vs
   assumed costs from the journal's equity series vs backtest
   expectations; costs can only hurt, so any divergence is bad news by
   definition.
9. **Single-asset, single-venue concentration.** NEAR-specific shocks,
   USDT quote risk, Binance operational risk, geo-blocking. A second venue
   or asset is out of scope for this design; the exposure cap (40%) is the
   only in-system mitigant.
10. **Behavioural failure of the human layer.** Retroactive note editing
    (append-only design makes it detectable, not impossible), decision
    frequency creeping past 2/day (the journal report shows the frequency
    distribution — the human is measured on discipline too), or
    rage-disarming after the stop that saved the month. This is the
    layer's known attack surface; the journal exists precisely so that the
    evidence, not the mood, decides whether the layer stays.

## 11. Implementation order and deferred work

Order (each step shippable and green on its own):

1. Phase A data (§3) + gap detection + tests. — *done*
2. `stats.py` + `journal.py` + `arm`/`disarm`/`journal report`/`status` CLI
   (usable immediately on the testnet — measurement starts before the
   overlay exists). — *done*
3. `overlay.py` + `backtest.py` overlay path + hand-computed tests. — *done*
4. Executor integration + restart reconciliation. — *done*
5. `near-bot validate` battery (multi-year, multi-symbol, bootstrap, DSR,
   N_eff) and `experiments/regime_sweep.py`. — *validate done; regime sweep pending*
6. Docs sweep (AGENTS.md, README, DEPLOY) and the simulator's
   "signal engine only" note. — *done*

Implementation notes (decisions the text above left open):

- **Leaving STOPPED re-anchors the full stop.** After a −10% flatten the
  account sits *at* the stop line, so measured from the old HWM any re-arm
  would re-stop on the next evaluation. Any human call out of STOPPED
  (`arm` or `disarm`) therefore measures the full stop from the equity at the
  next evaluation until equity makes a new high; the half-size tier keeps
  using the true HWM, which yields §4.3's composed tier × cooldown example.
- **`armed` edges are human calls in the backtester.** False→True runs
  `overlay.arm` (cooldown, lifts STOPPED) and True→False `overlay.disarm`;
  `armed_before` gives the permission just before the first bar.

Explicitly deferred (named so they are not silently done): JS port of the
overlay and browser-simulator parity for overlay paths; promoting BTC
features into `score.compute()`; short side via USD-M futures (the v2
roadmap item — unchanged); multi-bot deployment; any tuning of overlay
thresholds against the validation data.

---

*Not financial advice. Everything above is a research design; the only
number this project endorses is a validated one, and the validation rules in
§7 apply to this document's own claims.*

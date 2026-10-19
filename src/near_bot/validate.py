"""The validation battery: multi-symbol walk-forward with honest statistics.

Why a battery, not a command
----------------------------
v2's own README already concedes "not distinguishable from noise at this
sample size" on 91 days. Hardening into Trend Rider changes the data
(multi-year fixed windows, several symbols) and the machinery (risk overlay),
but not the epistemics: a number only counts if its uncertainty band excludes
zero after charging for everything we tried. ``near-bot validate`` runs this
module over every symbol of one fixed dated window and assembles, per symbol
and for the pooled cross-symbol portfolio:

* walk-forward OOS returns (Sharpe-selected, top-k ensemble — unchanged),
* block-bootstrap 95% CIs on Sharpe, annualised return and max drawdown
  (over the stitched per-bar OOS returns), and on expectancy per *decision*
  — the resampling unit for expectancy is the independent decision cluster,
  never the individual trade or the ensemble leg (DESIGN.md §7.2, §7.5),
* the deflated Sharpe ratio: N from the trials ledger (stats.TRIALS_TRIED),
  and the trial-Sharpe variance V from every grid cell traded as a fixed
  configuration *on the same OOS bars* as the tested series (see
  ``fixed_config_returns`` for why the sample length has to match),
* N_eff — independent decisions after clustering overlapping trades — next
  to every trade count,
* the OOS results conditioned on BTC regime (DESIGN.md §3.4): an altcoin
  edge that only exists while BTC trends up is BTC beta wearing a costume,
  and this is where that gets found out,
* a mandatory plain-words verdict (DESIGN.md §7.6 — the noise clause is the
  deliverable when true).

Two separations are deliberate. BTC is a *context* stream: it gets its own
block and supplies the regime labels, but stays out of the pooled portfolio,
so market beta cannot masquerade as diversification. And window size is a
researcher degree of freedom, so the CLI runs both the 90d/30d and the
30d/10d variants and reports both — choosing the flattering one afterwards
would be one more lottery ticket.

The regime labels are attribution, not a signal: each bar is bucketed by the
label known at the *previous* close, so "BTC was trending up" can never mean
"BTC went up on this very bar".
"""

from __future__ import annotations

import copy
import datetime as dt
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest as bt
from . import indicators, stats
from . import score as score_mod
from . import walkforward as wf

NOISE_SENTENCE = ("This result is not statistically distinguishable from noise "
                  "at this sample size.")
N_EFF_TARGET = 100       # DESIGN.md §7.5: N_eff "in the hundreds" or it is thin
DSR_CONFIDENCE = 0.95    # P(true SR > SR*) required to call an edge established
CONTEXT_SYMBOL = "BTCUSDT"
REGIMES = ("up", "chop", "down")


# -------------------------------------------------------------------- inputs

def locate_csvs(symbols, data_dir, start: str, end: str = ""):
    """Find each symbol's dated-window CSV, enforcing one identical window.

    With ``end`` given the file name is exact. Without it, the newest
    ``{SYMBOL}_1m_{START}_*.csv`` of the first symbol that has one fixes the
    window tag and every other symbol must match it — a battery over
    misaligned windows compares different markets, not different symbols.
    Returns ``(tag, found, missing)``; ``missing`` maps each absent symbol to
    the path that was looked for, so the report can name it.
    """
    data_dir = Path(data_dir)
    start_tag = dt.date.fromisoformat(start).strftime("%Y%m%d")
    tag = None
    if end:
        tag = f"{start_tag}_{dt.date.fromisoformat(end):%Y%m%d}"
    else:
        for symbol in symbols:
            cands = sorted(data_dir.glob(f"{symbol}_1m_{start_tag}_*.csv"))
            if cands:
                tag = cands[-1].stem.split("_1m_", 1)[1]
                break
    found: dict[str, Path] = {}
    missing: dict[str, Path] = {}
    for symbol in symbols:
        path = data_dir / f"{symbol}_1m_{tag or start_tag + '_*'}.csv"
        if tag and path.exists():
            found[symbol] = path
        else:
            missing[symbol] = path
    return tag, found, missing


def timestamped(res: wf.WalkForwardResult, bars: pd.DataFrame) -> pd.Series:
    """Stitched OOS returns re-indexed from bar positions to bar timestamps."""
    if not len(res.oos_returns):
        return pd.Series(dtype=float)
    ts = bars["timestamp"].reset_index(drop=True)
    return pd.Series(res.oos_returns.to_numpy(),
                     index=pd.DatetimeIndex(ts.iloc[list(res.oos_returns.index)]))


def pooled_returns(per_symbol: dict[str, pd.Series]) -> pd.Series:
    """Cross-symbol per-bar mean of timestamped OOS returns.

    Windows are calendar-aligned across symbols (same fixed window), so the
    pooled series is the equity path of running the bot on all symbols at
    once with capital split equally across the active ones.
    """
    parts = {s: r for s, r in per_symbol.items() if len(r)}
    if not parts:
        return pd.Series(dtype=float)
    return pd.DataFrame(parts).mean(axis=1, skipna=True).sort_index()


# -------------------------------------------------------------- BTC regime

def btc_regime(bars: pd.DataFrame, p) -> pd.Series:
    """Causal market-regime label per bar, indexed by timestamp.

    ``chop`` when Kaufman ER < ``er_min``, otherwise ``up``/``down`` against
    the ``trend_window`` SMA — the same two gates the engine applies to the
    traded symbol, read off BTC. NaN while the indicators warm up.
    """
    close = bars["close"].reset_index(drop=True)
    ma = indicators.sma(close, p.trend_window)
    er = indicators.efficiency_ratio(close, p.er_window)
    label = pd.Series(np.where(er < p.er_min, "chop",
                               np.where(close > ma, "up", "down")), dtype=object)
    label = label.where(ma.notna() & er.notna())
    return pd.Series(label.to_numpy(), index=pd.DatetimeIndex(bars["timestamp"]),
                     name="btc_regime")


def condition_on_regime(returns: pd.Series, regime: pd.Series,
                        bar_minutes: int) -> dict:
    """Bucket timestamped OOS returns by the regime known before each bar.

    The label is lagged one bar, so a bar's return is attributed to the
    regime visible at the previous close. Bars with no label (warmup, or no
    BTC bar at that timestamp) are counted, not dropped silently.
    """
    lagged = regime.shift(1).reindex(returns.index)
    out: dict = {"unlabelled": int(lagged.isna().sum()), "n_total": len(returns)}
    sharpe = stats.sharpe_stat(bar_minutes)
    for r in REGIMES:
        x = returns[lagged == r].to_numpy(dtype=float)
        out[r] = {
            "n_bars": len(x),
            "share": len(x) / len(returns) if len(returns) else 0.0,
            "total_return": stats.total_return_stat(x) if len(x) else 0.0,
            "sharpe": sharpe(x) if len(x) > 1 else 0.0,
        }
    return out


# ---------------------------------------------------------------- statistics

def evaluate(oos, trades, *, bar_minutes: int, top_k: int, n_boot: int, block: int,
             seed: int = 7, trial_sharpes=(), n_trials: int = stats.TRIALS_TRIED,
             pnl_scale: float | None = None) -> dict:
    """Bootstrap CIs, deflated Sharpe, N_eff and the verdict for one OOS path.

    ``oos`` is the stitched per-bar OOS return series; ``trades`` are the OOS
    trades behind it (every ensemble leg, every pooled symbol) — clustered
    into independent decisions before any expectancy statistic. ``pnl_scale``
    turns a leg's ``weighted_net`` into its portfolio contribution (default
    1/top_k: each ensemble leg trades 1/k of the capital).
    """
    oos = np.asarray(oos, dtype=float)
    dc = stats.decision_clusters(trades)
    out: dict = {"n_bars": len(oos), "n_trades": len(trades), "n_eff": dc.n_eff}
    if len(oos) < 10:
        out.update(noise=True, verdict=f"{NOISE_SENTENCE} (insufficient OOS data)")
        return out

    out["sharpe"] = stats.bootstrap_ci(
        oos, stats.sharpe_stat(bar_minutes), n_boot, block, seed)
    out["ann_return"] = stats.bootstrap_ci(
        oos, stats.ann_return_stat(bar_minutes), n_boot, block, seed + 1)
    out["max_drawdown"] = stats.bootstrap_ci(
        oos, stats.max_drawdown_stat, n_boot, block, seed + 3)
    out["total_return"] = stats.total_return_stat(oos)

    # Clusters are already the independent unit, so they are resampled one at
    # a time (block 1). A longer circular block over a handful of clusters
    # degenerates: with block == n every resample is a rotation of the sample
    # and the "CI" collapses onto the point estimate.
    pnls = dc.cluster_pnls(scale=1.0 / top_k if pnl_scale is None else pnl_scale)
    if len(pnls) >= 3:
        out["expectancy_per_decision"] = stats.bootstrap_ci(
            pnls, stats.mean_stat, n_boot, 1, seed + 2)
    else:
        out["expectancy_per_decision"] = (
            float(pnls.mean()) if len(pnls) else 0.0, math.nan, math.nan)

    sd = oos.std()
    sr_bar = float(oos.mean() / sd) if sd > 0 else 0.0
    dsr = stats.deflated_sharpe(sr_bar, len(oos), trial_sharpes,
                                skew=stats.skewness(oos), kurt=stats.kurtosis(oos),
                                n_trials=n_trials)
    ann = math.sqrt(indicators.bars_per_year(bar_minutes))
    out.update(dsr=dsr["dsr"], dsr_benchmark=dsr["sr_star"] * ann,
               dsr_excess=(sr_bar - dsr["sr_star"]) * ann, dsr_trials=dsr["n_trials"])
    out["noise"], out["verdict"] = _verdict(out)
    return out


def _verdict(b: dict) -> tuple[bool, str]:
    """The noise clause (DESIGN.md §7.6), applied mechanically.

    Noise when any headline CI includes zero, when the deflated Sharpe does
    not beat the selection-bias benchmark, or when N_eff is too small for an
    expectancy CI to exist at all. Short of noise, fewer than N_EFF_TARGET
    independent decisions is *thin* (§7.5): a bootstrap over a few dozen
    decisions can exclude zero by luck, so it is never called established.
    """
    reasons = []
    for key, label in (("sharpe", "Sharpe"), ("ann_return", "annualised-return")):
        lo = b[key][1]
        if not (np.isfinite(lo) and lo > 0.0):
            reasons.append(f"{label} CI includes zero")
    e_lo = b["expectancy_per_decision"][1]
    if not np.isfinite(e_lo):
        reasons.append(f"N_eff = {b['n_eff']} is too few decisions for an expectancy CI")
    elif e_lo <= 0.0:
        reasons.append("per-decision expectancy CI includes zero")
    if b["dsr_excess"] <= 0.0:
        reasons.append("deflated Sharpe <= 0 (no excess over the selection-bias benchmark)")
    if reasons:
        return True, f"{NOISE_SENTENCE} ({'; '.join(reasons)})"
    if b["n_eff"] < N_EFF_TARGET:
        return False, (f"CIs exclude zero but N_eff = {b['n_eff']} < {N_EFF_TARGET}: "
                       "statistically thin - not established")
    if b["dsr"] < DSR_CONFIDENCE:
        return False, (f"CIs exclude zero but P(SR > SR*) = {b['dsr']:.2f} < "
                       f"{DSR_CONFIDENCE}: not established after charging for "
                       f"{b['dsr_trials']} configurations tried")
    return False, ("CIs exclude zero and survive deflation - provisionally "
                   "established, keep measuring")


# ScoreParams fields that change indicators.add_all output (cache key).
_INDICATOR_FIELDS = ("mom_fast", "mom_slow", "vol_window", "bo_window", "ofi_window",
                     "er_window", "trend_window", "atr_period")


def _per_bar_sharpe(x) -> float:
    x = np.asarray(x, dtype=float)
    sd = x.std()
    return float(x.mean() / sd) if sd > 0 else 0.0


def fixed_config_returns(bars: pd.DataFrame, cfg, oos_index, overlay=None,
                         grid: dict | None = None) -> dict[tuple, pd.Series]:
    """Every grid cell traded as one fixed configuration, on the OOS bars only.

    Why the deflated Sharpe needs these, not the in-sample window Sharpes
    --------------------------------------------------------------------
    The benchmark SR* = √V · f(N) is the Sharpe the best of N worthless trials
    would show by luck, and V must be the variance of trial Sharpes estimated
    on a sample as long as the tested one — a per-bar Sharpe's estimation
    noise scales as 1/√T. The first real-data run (e0d6f3c) took V from
    in-sample window Sharpes (720-2,160 bars) and set it against a
    ~47,000-bar OOS Sharpe: the short windows' noise alone put SR* at +6 to
    +13 annualised, a bar no strategy could clear. Trading each cell as a
    fixed configuration over the same OOS bars puts trials and tested series
    on the same T, which is what Bailey & López de Prado's benchmark assumes.
    N still comes from the trials ledger (DESIGN.md §7.3).

    Returns ``{combo key: per-bar returns indexed by OOS bar timestamp}``.
    """
    grid = grid or wf.DEFAULT_GRID
    idx = np.asarray(list(oos_index), dtype=int)
    ts = pd.DatetimeIndex(bars["timestamp"].reset_index(drop=True).iloc[idx])
    frames: dict[tuple, pd.DataFrame] = {}
    out: dict[tuple, pd.Series] = {}
    for combo in wf._combos(grid):
        params = replace(cfg.strategy, **combo)
        key = tuple(getattr(params, f) for f in _INDICATOR_FIELDS)
        if key not in frames:
            frames[key] = indicators.add_all(bars, params)
        frame = frames[key]
        sub = copy.copy(cfg)
        sub.strategy = params
        res = bt.run(frame, sub, score_mod.compute(frame, params), overlay=overlay)
        out[tuple(sorted(combo.items()))] = pd.Series(res.bar_returns.to_numpy()[idx],
                                                      index=ts)
    return out


def run_battery(bars_by_symbol: dict[str, pd.DataFrame], cfg, *, train_bars: int,
                test_bars: int, top_k: int, n_boot: int, block: int, seed: int = 7,
                overlay=None, context_symbol: str = CONTEXT_SYMBOL) -> dict:
    """Walk-forward plus statistics for every symbol and the pooled portfolio.

    Returns ``{"symbols": {sym: block}, "pooled": block | None,
    "results": {sym: WalkForwardResult}}``. The context symbol gets its own
    block but stays out of the pool, and — when its bars are present —
    supplies the regime labels every block is conditioned on. A symbol whose
    history cannot fit one train+test window gets a noise verdict, not a
    silent omission.
    """
    bm = cfg.aggregate_minutes
    regime = (btc_regime(bars_by_symbol[context_symbol], cfg.strategy)
              if context_symbol in bars_by_symbol else None)
    blocks: dict[str, dict] = {}
    results: dict[str, wf.WalkForwardResult] = {}
    returns: dict[str, pd.Series] = {}
    fixed: dict[str, dict[tuple, pd.Series]] = {}
    for symbol, bars in bars_by_symbol.items():
        try:
            res = wf.run(bars, cfg, train_bars=train_bars, test_bars=test_bars,
                         top_k=top_k, overlay=overlay)
        except ValueError as exc:
            blocks[symbol] = {"n_trades": 0, "n_eff": 0, "noise": True,
                              "verdict": f"{NOISE_SENTENCE} ({exc})"}
            continue
        results[symbol] = res
        returns[symbol] = timestamped(res, bars)
        if len(res.oos_returns) >= 10:
            fixed[symbol] = fixed_config_returns(bars, cfg, res.oos_returns.index,
                                                 overlay=overlay)
        blk = evaluate(res.oos_returns.to_numpy(), res.oos_trades, bar_minutes=bm,
                       top_k=top_k, n_boot=n_boot, block=block, seed=seed,
                       trial_sharpes=[_per_bar_sharpe(r) for r in
                                      fixed.get(symbol, {}).values()],
                       n_trials=stats.TRIALS_TRIED)
        blk["windows"] = len(res.windows)
        blk["windows_traded"] = sum(1 for w in res.windows if w.combos is not None)
        if regime is not None and len(returns[symbol]):
            blk["regime"] = condition_on_regime(returns[symbol], regime, bm)
        blocks[symbol] = blk

    pooled = None
    pool = [s for s in results if s != context_symbol and s in fixed]
    if len(pool) >= 2:
        pr = pooled_returns({s: returns[s] for s in pool})
        # each trial = one grid cell traded fixed on every pooled symbol at once
        trials = [_per_bar_sharpe(pooled_returns({s: fixed[s][k] for s in pool}))
                  for k in fixed[pool[0]]]
        pooled = evaluate(pr.to_numpy(), [t for s in pool for t in results[s].oos_trades],
                          bar_minutes=bm, top_k=top_k, n_boot=n_boot, block=block,
                          seed=seed, trial_sharpes=trials, n_trials=stats.TRIALS_TRIED,
                          pnl_scale=1.0 / (top_k * len(pool)))
        pooled["members"] = pool
        if regime is not None:
            pooled["regime"] = condition_on_regime(pr, regime, bm)
    return {"symbols": blocks, "pooled": pooled, "results": results}


# ------------------------------------------------------------------- report

def _ci(v: tuple, fmt: str) -> str:
    pt, lo, hi = v
    return f"{pt:{fmt}}  95% CI [{lo:{fmt}}, {hi:{fmt}}]"


def _format_regime(reg: dict) -> list[str]:
    lines = ["- BTC regime at the previous close (descriptive, not a signal):"]
    for r in REGIMES:
        d = reg[r]
        lines.append(f"  - {r:<4}: {d['n_bars']:>6,} bars ({d['share']:.0%}), "
                     f"return {d['total_return']:+.2%}, Sharpe {d['sharpe']:+.2f}")
    if reg["unlabelled"]:
        lines.append(f"  - unlabelled: {reg['unlabelled']:,} bars (BTC warmup or no BTC bar)")
    return lines


def format_block(name: str, block: dict) -> list[str]:
    lines = [f"### {name}", ""]
    if "sharpe" not in block:
        lines += [(f"- OOS trades {block.get('n_trades', 0)}, "
                   f"independent decisions N_eff = {block.get('n_eff', 0)}"),
                  f"- **Verdict: {block['verdict']}**", ""]
        return lines
    lines += [
        (f"- OOS bars {block['n_bars']:,}, trades {block['n_trades']}, "
         f"**independent decisions N_eff = {block['n_eff']}**"),
        f"- Sharpe (ann.): {_ci(block['sharpe'], '+.2f')}",
        f"- Annualised return: {_ci(block['ann_return'], '+.2%')}",
        f"- Max drawdown: {_ci(block['max_drawdown'], '.2%')}",
        f"- Total return: {block['total_return']:+.2%}",
        f"- Expectancy / decision: {_ci(block['expectancy_per_decision'], '+.3%')}",
        (f"- Deflated Sharpe: P(SR > SR*) = {block['dsr']:.3f}, excess over "
         f"benchmark {block['dsr_excess']:+.2f} (SR* {block['dsr_benchmark']:+.2f} "
         f"ann., N trials {block['dsr_trials']})"),
    ]
    if "windows" in block:
        lines.append(f"- Windows: {block['windows_traded']}/{block['windows']} traded")
    if block.get("regime"):
        lines += _format_regime(block["regime"])
    if block.get("plot"):
        lines.append(f"- OOS equity: ![{name}]({block['plot']})")
    lines.append(f"- **Verdict: {block['verdict']}**")
    if block["noise"] and block["n_eff"] < N_EFF_TARGET:
        lines.append(f"- N_eff = {block['n_eff']} < {N_EFF_TARGET}: even a cleaner "
                     "result would remain statistically thin.")
    lines.append("")
    return lines


def _ordered_blocks(battery: dict, production: str, context_symbol: str):
    """(name, block, counts_for_overall) in report order: production symbol,
    other traded symbols, the pool, then the context stream."""
    syms = battery["symbols"]
    traded = sorted((s for s in syms if s != context_symbol),
                    key=lambda s: (s != production, s))
    for s in traded:
        yield (f"{s} (production)" if s == production else s), syms[s], True
    if battery.get("pooled"):
        members = " + ".join(battery["pooled"]["members"])
        yield (f"POOLED (equal weight: {members}; "
               f"{context_symbol} excluded)"), battery["pooled"], True
    if context_symbol in syms:
        yield (f"{context_symbol} (context stream, kept out of the pool)",
               syms[context_symbol], False)


def verdict_lines(variants: list[tuple[str, dict]], production: str,
                  context_symbol: str = CONTEXT_SYMBOL) -> list[str]:
    """The mandatory closing verdict section (DESIGN.md §7.6)."""
    lines = ["## Verdict", ""]
    counted = []
    for label, battery in variants:
        for name, block, counts in _ordered_blocks(battery, production, context_symbol):
            lines.append(f"- {label} | {name}: {block['verdict']}")
            if counts:
                counted.append(block)
    lines.append("")
    if not counted or any(b.get("noise") for b in counted):
        lines.append(f"**Overall: {NOISE_SENTENCE}**")
    elif any(b["n_eff"] < N_EFF_TARGET for b in counted):
        lines.append(f"**Overall: not established - every CI excludes zero, but at "
                     f"least one block has fewer than {N_EFF_TARGET} independent "
                     f"decisions (statistically thin).**")
    elif any(b.get("dsr", 0.0) < DSR_CONFIDENCE for b in counted):
        lines.append("**Overall: not established - every CI excludes zero, but at "
                     "least one block fails deflation after charging for the "
                     "configurations tried.**")
    else:
        lines.append("**Overall: provisionally established in every block - keep "
                     "measuring; a validated edge is still not a promise.**")
    return lines


def render_report(variants: list[tuple[str, dict]], header: list[str],
                  production: str, context_symbol: str = CONTEXT_SYMBOL) -> str:
    lines = ["# Validation report", "", *header, ""]
    for label, battery in variants:
        lines += [f"## Window variant: {label}", ""]
        for name, block, _ in _ordered_blocks(battery, production, context_symbol):
            lines += format_block(name, block)
    lines += verdict_lines(variants, production, context_symbol)
    lines += [
        "",
        "---",
        ("Noise clause (DESIGN.md §7.6): any block whose CI includes zero, whose "
         "deflated Sharpe does not beat the selection-bias benchmark, or whose "
         "N_eff is too small for an expectancy CI is not statistically "
         "distinguishable from noise at this sample size, and is reported as "
         "such. DSR charges for every configuration tried (stats.TRIALS_TRIED)."),
        "",
        "Not financial advice.",
    ]
    return "\n".join(lines)

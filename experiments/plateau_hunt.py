#!/usr/bin/env python3
"""Pre-registered plateau hunt: one engine configuration, chosen by rule.

Why pre-register a parameter search
-----------------------------------
The first real-data validation (e0d6f3c) found the default engine not
distinguishable from noise. The tempting next move — try settings until one
looks good — is exactly how backtests lie: with enough configurations,
*something* always clears the bar. So everything that decides the outcome is
fixed in this file and committed *before* a single number is computed: the
grid, the score, the eligibility gate, the plateau rule, the stability rule
that can veto it, and the holdout protocol. Every one of the 125 cells is
charged to the trials ledger (stats.TRIALS_TRIED) whatever it scores.

Why a plateau, not a peak
-------------------------
The best single cell of a grid is mostly the luckiest one. A setting worth
keeping sits on a plateau: its neighbours do nearly as well, so the result
does not hinge on the exact value. The selected configuration is therefore
the eligible cell with the best *neighbourhood* score — the mean over itself
and every cell within one grid step on every knob. Averaging over neighbours
is also the mitigation for ranking noise, by design.

The funnel (decided 2026-10-19)
-------------------------------
(a) Plateau hunt on the full 2021-01-01 -> 2024-12-31 span, NEAR/ETH/SOL.
    A compute-budget amendment proposed scoring on a 10% random-block
    subsample; it was not used: a full-span fixed-config backtest costs
    ~0.25 s per config and symbol, and 20 x 7.3-day intervals would have
    cut the evidence ~10x (about 23 trades per config and symbol instead of
    ~228) while forcing long-holding configurations flat at every interval
    end — a bias against exactly the trades a trend-follower lives on.
(b) Stability check: the same rule on 2021-22 and on 2023-24 (the continuous
    2021-24 run's per-bar returns, split at 2023-01-01). If (b) contradicts
    (a) — in either half the selected cell scores <= 0, or its neighbourhood
    score falls outside the top 25% of eligible cells — the selection is
    reported as unstable. It is not rescued.
(c) Only after explicit human approval: the selected cell, fixed, on the
    holdout 2025-01-01 -> 2026-09-30. Status: viewed in aggregate in e0d6f3c
    (that run's walk-forward reports and equity plots covered these months),
    so it is not a pristine holdout and is labelled as such. Pre-registered
    test: the pooled equal-weight holdout returns pass only if no noise-clause
    reason fires (Sharpe, annualised-return and per-decision expectancy CIs
    all exclude zero; Sharpe above zero).

    python experiments/plateau_hunt.py                       # (a) + (b)
    python experiments/plateau_hunt.py --holdout --center theta_in=0.25 \\
        trail_atr=3.0 er_min=0.2                             # (c), after approval
"""

from __future__ import annotations

import argparse
import copy
import math
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
import pandas as pd

from near_bot import backtest as bt
from near_bot import data, indicators, stats, validate
from near_bot import score as score_mod
from near_bot import walkforward as wf
from near_bot.config import Config

# ---------------------------------------------------- the pre-registered spec
SYMBOLS = ("NEARUSDT", "ETHUSDT", "SOLUSDT")
SEARCH = ("2021-01-01", "2025-01-01")                 # [start, end): 2021-2024
HALVES = (("2021-01-01", "2023-01-01"), ("2023-01-01", "2025-01-01"))
HOLDOUT = ("2025-01-01", "2026-10-01")                # [start, end)
HOLDOUT_STATUS = "viewed in aggregate in e0d6f3c"
DATA_WINDOW = ("2021-01-01", "2026-10-01")           # the dated CSVs to load
GRID = {
    "theta_in": [0.15, 0.20, 0.25, 0.30, 0.35],
    "trail_atr": [2.0, 2.5, 3.0, 3.5, 4.0],
    "er_min": [0.10, 0.15, 0.20, 0.25, 0.30],
}
MIN_TRADES = 30          # per symbol over the 4-year search span
MIN_TRADES_HALF = 15     # per symbol per 2-year half (same rate)
STABILITY_TOP_FRAC = 0.25
SEED = 7


# ------------------------------------------------------------------- loading

def load_bars(data_dir, cfg) -> dict[str, pd.DataFrame]:
    """60m bars per symbol from the dated-window CSVs (full window)."""
    _, found, missing = validate.locate_csvs(SYMBOLS, data_dir, *DATA_WINDOW)
    if missing:
        raise SystemExit(f"missing data for {', '.join(missing)}: {list(missing.values())}")
    return {s: data.aggregate(data.load_csv(found[s]), cfg.aggregate_minutes,
                              drop_incomplete=True) for s in SYMBOLS}


def search_slice(bars: pd.DataFrame) -> pd.DataFrame:
    """The 2021-2024 search span. Hard guard: no holdout bar ever enters (a)/(b)."""
    ts = bars["timestamp"]
    out = bars[(ts >= SEARCH[0]) & (ts < SEARCH[1])].reset_index(drop=True)
    assert out["timestamp"].max() < pd.Timestamp(HOLDOUT[0]), "holdout leak"
    return out


# ------------------------------------------------------------------ the grid

def cells(grid: dict) -> list[dict]:
    return wf._combos(grid)


def run_grid(bars_by_symbol: dict[str, pd.DataFrame], cfg, grid: dict) -> dict:
    """Backtest every cell, as a fixed configuration, on every symbol.

    Indicators are causal and computed once per distinct indicator setting
    (the grid's knobs do not change them); the score is recomputed per cell.
    """
    combos = cells(grid)
    out = {"cells": combos, "returns": {}, "trades": {}, "timestamps": {}}
    for symbol, bars in bars_by_symbol.items():
        frames: dict[tuple, pd.DataFrame] = {}
        rets, trades = [], []
        for combo in combos:
            params = replace(cfg.strategy, **combo)
            key = tuple(getattr(params, f) for f in validate._INDICATOR_FIELDS)
            if key not in frames:
                frames[key] = indicators.add_all(bars, params)
            frame = frames[key]
            sub = copy.copy(cfg)
            sub.strategy = params
            res = bt.run(frame, sub, score_mod.compute(frame, params))
            rets.append(res.bar_returns.to_numpy())
            trades.append(res.trades)
        out["returns"][symbol] = rets
        out["trades"][symbol] = trades
        out["timestamps"][symbol] = pd.DatetimeIndex(bars["timestamp"].reset_index(drop=True))
    return out


def _sharpe(x) -> float:
    return validate._per_bar_sharpe(x)


def score_cells(run: dict, min_trades: int, span: tuple[str, str] | None = None) -> dict:
    """Score per cell = mean over symbols of the per-bar Sharpe (flat bars
    included); eligible = every symbol has >= ``min_trades`` trades entered in
    the span. ``span`` restricts both to [start, end) of the continuous run."""
    n = len(run["cells"])
    per_symbol = {}
    counts = {}
    for symbol, rets in run["returns"].items():
        ts = run["timestamps"][symbol]
        mask = (np.ones(len(ts), dtype=bool) if span is None else
                np.asarray((ts >= span[0]) & (ts < span[1])))
        per_symbol[symbol] = np.array([_sharpe(r[mask]) for r in rets])
        lo = pd.Timestamp(span[0]) if span else None
        hi = pd.Timestamp(span[1]) if span else None
        counts[symbol] = np.array([
            sum(1 for t in tr if span is None or lo <= pd.Timestamp(t.entry_time) < hi)
            for tr in run["trades"][symbol]])
    scores = np.mean([per_symbol[s] for s in per_symbol], axis=0)
    eligible = np.all([counts[s] >= min_trades for s in counts], axis=0)
    assert len(scores) == n
    return {"scores": scores, "eligible": eligible, "per_symbol": per_symbol, "trades": counts}


# ---------------------------------------------------------- the plateau rule

def neighbourhood_scores(scores: np.ndarray, shape: tuple[int, ...]) -> np.ndarray:
    """Mean raw score over each cell and every cell within one grid step on
    every knob (edges average over the neighbours that exist)."""
    s = np.asarray(scores, dtype=float).reshape(shape)
    out = np.empty_like(s)
    for idx in np.ndindex(shape):
        out[idx] = s[tuple(slice(max(0, i - 1), i + 2) for i in idx)].mean()
    return out.ravel()


def select_center(neigh: np.ndarray, eligible: np.ndarray) -> int | None:
    """Eligible cell with the best neighbourhood score; ties -> grid order."""
    cand = np.flatnonzero(eligible)
    if not len(cand):
        return None
    return int(cand[np.argmax(neigh[cand])])


def boundary_knobs(index: int, grid: dict) -> list[str]:
    shape = tuple(len(v) for v in grid.values())
    pos = np.unravel_index(index, shape)
    return [k for k, p, size in zip(grid, pos, shape, strict=True) if p in (0, size - 1)]


def top_fraction(neigh: np.ndarray, eligible: np.ndarray, index: int) -> float:
    """Share of eligible cells with a strictly better neighbourhood score
    (0.0 = best). An ineligible index counts as outside the ranking (1.0)."""
    if not eligible[index]:
        return 1.0
    pool = neigh[eligible]
    return float((pool > neigh[index]).sum() / len(pool))


def rank_correlation(a, b) -> float:
    """Spearman rank correlation (no scipy dependency)."""
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return math.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def stability(center: int, halves: list[dict]) -> dict:
    """(b): contradiction if, in either half, the center's raw score <= 0 or
    its neighbourhood score ranks outside the top STABILITY_TOP_FRAC."""
    reasons = []
    for i, h in enumerate(halves):
        if h["scores"][center] <= 0.0:
            reasons.append(f"half {i + 1}: selected cell scores <= 0")
        if h["top_fraction"] >= STABILITY_TOP_FRAC:
            reasons.append(f"half {i + 1}: neighbourhood score outside the top "
                           f"{STABILITY_TOP_FRAC:.0%} of eligible cells")
    return {"contradicted": bool(reasons), "reasons": reasons}


# ---------------------------------------------------------------- the hunt

def hunt(bars_by_symbol: dict[str, pd.DataFrame], cfg, grid: dict = GRID,
         min_trades: int = MIN_TRADES, halves=HALVES,
         min_trades_half: int = MIN_TRADES_HALF, n_boot: int = 10000,
         block: int = 168) -> dict:
    """(a) select the plateau center on the whole span, (b) check it per half."""
    shape = tuple(len(v) for v in grid.values())
    run = run_grid(bars_by_symbol, cfg, grid)
    full = score_cells(run, min_trades)
    full["neigh"] = neighbourhood_scores(full["scores"], shape)
    center = select_center(full["neigh"], full["eligible"])
    result = {"run": run, "full": full, "center": center, "shape": shape, "grid": grid}
    if center is None:
        return result

    half_results = []
    for span in halves:
        h = score_cells(run, min_trades_half, span)
        h["neigh"] = neighbourhood_scores(h["scores"], shape)
        h["span"] = span
        h["own_center"] = select_center(h["neigh"], h["eligible"])
        h["top_fraction"] = top_fraction(h["neigh"], h["eligible"], center)
        half_results.append(h)
    result["halves"] = half_results
    result["halves_rank_corr"] = (rank_correlation(half_results[0]["neigh"],
                                                   half_results[1]["neigh"])
                                  if len(half_results) == 2 else math.nan)
    result["stability"] = stability(center, half_results)

    # center statistics on the pooled equal-weight portfolio, full span
    syms = list(run["returns"])
    pooled = validate.pooled_returns({
        s: pd.Series(run["returns"][s][center], index=run["timestamps"][s]) for s in syms})
    trials = [_sharpe(validate.pooled_returns({
        s: pd.Series(run["returns"][s][c], index=run["timestamps"][s]) for s in syms}))
        for c in range(len(run["cells"]))]
    result["center_block"] = validate.evaluate(
        pooled.to_numpy(), [t for s in syms for t in run["trades"][s][center]],
        bar_minutes=cfg.aggregate_minutes, top_k=1, n_boot=n_boot, block=block,
        seed=SEED, trial_sharpes=trials, n_trials=stats.TRIALS_TRIED,
        pnl_scale=1.0 / len(syms))
    return result


def holdout(full_bars: dict[str, pd.DataFrame], cfg, combo: dict, n_boot: int = 10000,
            block: int = 168) -> dict:
    """(c) The selected cell, fixed, on the holdout only. Indicators run on the
    whole causal history; trading starts flat at the first holdout bar."""
    params = replace(cfg.strategy, **combo)
    sub = copy.copy(cfg)
    sub.strategy = params
    rets, trades, per_symbol = {}, [], {}
    for symbol, bars in full_bars.items():
        bars = bars[bars["timestamp"] < HOLDOUT[1]].reset_index(drop=True)
        frame = indicators.add_all(bars, params)
        score = score_mod.compute(frame, params)
        a = int(np.searchsorted(frame["timestamp"].to_numpy(),
                                np.datetime64(pd.Timestamp(HOLDOUT[0]))))
        res = bt.run(frame.iloc[a:], sub, score.iloc[a:])
        rets[symbol] = pd.Series(res.bar_returns.to_numpy(),
                                 index=pd.DatetimeIndex(frame["timestamp"].iloc[a:]))
        trades.extend(res.trades)
        per_symbol[symbol] = (_sharpe(res.bar_returns), len(res.trades))
    pooled = validate.pooled_returns(rets)
    blk = validate.evaluate(pooled.to_numpy(), trades, bar_minutes=cfg.aggregate_minutes,
                            top_k=1, n_boot=n_boot, block=block, seed=SEED,
                            trial_sharpes=(), n_trials=1, pnl_scale=1.0 / len(rets))
    return {"block": blk, "per_symbol": per_symbol}


# ------------------------------------------------------------------- report

def _ann(cfg) -> float:
    return math.sqrt(indicators.bars_per_year(cfg.aggregate_minutes))


def _combo_str(combo: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in combo.items())


def _git_state() -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "experiments"],
                               capture_output=True, text=True, check=True).stdout.strip()
        return f"{head} ({'uncommitted changes in src/experiments' if dirty else 'clean'})"
    except Exception:  # noqa: BLE001 - provenance is best-effort
        return "unknown"


def plateau_map(res: dict, cfg) -> list[str]:
    grid, full, center = res["grid"], res["full"], res["center"]
    knobs = list(grid)
    a, b, c = knobs
    ann = _ann(cfg)
    lines = [("Neighbourhood score (raw score in brackets), annualised Sharpe; "
              "`x` = ineligible (< min trades), **bold** = selected cell."), ""]
    shape = res["shape"]
    for k, vc in enumerate(grid[c]):
        lines += [f"**{c} = {vc}**", "",
                  f"| {a} \\ {b} | " + " | ".join(str(v) for v in grid[b]) + " |",
                  "|---|" + "---|" * len(grid[b])]
        for i, va in enumerate(grid[a]):
            row = []
            for j, _ in enumerate(grid[b]):
                idx = int(np.ravel_multi_index((i, j, k), shape))
                txt = f"{full['neigh'][idx] * ann:+.2f} ({full['scores'][idx] * ann:+.2f})"
                if not full["eligible"][idx]:
                    txt += " x"
                if idx == center:
                    txt = f"**{txt}**"
                row.append(txt)
            lines.append(f"| {va} | " + " | ".join(row) + " |")
        lines.append("")
    return lines


def render(res: dict, cfg) -> str:
    ann = _ann(cfg)
    grid, full, center = res["grid"], res["full"], res["center"]
    n_cells = len(res["run"]["cells"])
    lines = [
        "# Plateau hunt (pre-registered: experiments/plateau_hunt.py)", "",
        (f"- Code state at run time: {_git_state()} (spec and trials-ledger charge "
         "committed before this run)"),
        (f"- Search span: [{SEARCH[0]}, {SEARCH[1]}) (2021-2024), symbols "
         f"{', '.join(SYMBOLS)}, {cfg.aggregate_minutes}m bars, fixed-config backtests, "
         f"costs {cfg.costs.taker_fee:.3%} fee + {cfg.costs.slippage:.3%} slippage per side"),
        f"- Grid: {n_cells} cells — " + "; ".join(f"{k} {v}" for k, v in grid.items())
        + " (all other parameters at their defaults)",
        (f"- Score: mean over symbols of the per-bar Sharpe, net of costs; eligible = "
         f">= {MIN_TRADES} trades per symbol ({MIN_TRADES_HALF} per half)"),
        ("- Selection: eligible cell with the best neighbourhood score (mean over "
         "itself and all cells within one grid step on every knob)"),
        (f"- Trials ledger: stats.TRIALS_TRIED = {stats.TRIALS_TRIED}, every cell "
         "charged whatever it scored"),
        ("- Data: full span, not the proposed 10% random-block subsample (decision "
         "2026-10-19: ~0.25 s per config and symbol in full; the subsample would have cut "
         "evidence ~10x and forced long holds flat at every interval end). Neighbourhood "
         "averaging is the mitigation for ranking noise, by design."),
        (f"- Holdout {HOLDOUT[0]} -> 2026-09-30: NOT touched by this run; status "
         f"\"{HOLDOUT_STATUS}\""), "",
        "## (a) Plateau map, 2021-2024", "",
    ]
    lines += plateau_map(res, cfg)
    if center is None:
        lines += ["No eligible cell: nothing selected.", "", "Not financial advice."]
        return "\n".join(lines)

    combo = res["run"]["cells"][center]
    edges = boundary_knobs(center, grid)
    lines += ["## Selected plateau center", "",
              f"- **{_combo_str(combo)}**",
              (f"- Neighbourhood score {full['neigh'][center] * ann:+.2f}, raw score "
               f"{full['scores'][center] * ann:+.2f} (annualised Sharpe)"),
              "- Per symbol: " + "; ".join(
                  f"{s} Sharpe {full['per_symbol'][s][center] * ann:+.2f}, "
                  f"{full['trades'][s][center]} trades" for s in full["per_symbol"])]
    if edges:
        lines.append(f"- On the grid boundary in {', '.join(edges)}: the plateau may extend "
                     "beyond the tested range. The grid is not extended, by rule.")
    lines += ["", "## Selected cell, pooled equal-weight portfolio, 2021-2024 (in-sample)", "",
              ("This is the sample the cell was selected on. The deflated Sharpe charges for "
               "the selection (N from the ledger, V from all cells' pooled Sharpes on the same "
               "bars); only the holdout can confirm anything."), ""]
    lines += validate.format_block(_combo_str(combo), res["center_block"])

    st = res["stability"]
    lines += ["## (b) Stability check: 2021-22 vs 2023-24", "",
              ("| half | selected cell raw score | its neighbourhood score | rank (share of "
               "eligible cells ahead) | the half's own center |"), "|---|---|---|---|---|"]
    for h in res["halves"]:
        own = (_combo_str(res["run"]["cells"][h["own_center"]])
               if h["own_center"] is not None else "none eligible")
        lines.append(f"| {h['span'][0][:4]}-{int(h['span'][1][:4]) - 1} | "
                     f"{h['scores'][center] * ann:+.2f} | {h['neigh'][center] * ann:+.2f} | "
                     f"{h['top_fraction']:.0%}{'' if h['eligible'][center] else ' (ineligible)'}"
                     f" | {own} |")
    lines += ["", (f"- Rank correlation of neighbourhood scores between the halves "
                   f"(Spearman, all {n_cells} cells): {res['halves_rank_corr']:+.2f}"), ""]
    if st["contradicted"]:
        lines.append("- **(b) CONTRADICTS (a)**: " + "; ".join(st["reasons"])
                     + ". The selection is not stable across sub-periods; it is reported, "
                     "not rescued.")
    else:
        lines.append(f"- (b) consistent with (a): the selected cell scores > 0 and ranks in "
                     f"the top {STABILITY_TOP_FRAC:.0%} of eligible cells in both halves.")

    blk = res["center_block"]
    lines += ["", "## Verdict", "",
              f"- In-sample (2021-2024), selected cell: {blk['verdict']}",
              f"- Stability: {'CONTRADICTED' if st['contradicted'] else 'consistent'}"]
    if blk.get("noise") or st["contradicted"]:
        lines.append(f"\n**Overall: {validate.NOISE_SENTENCE}** No configuration from this "
                     "grid is established, in-sample or across sub-periods.")
    else:
        lines.append("\n**Overall: the selected cell survives the in-sample tests and the "
                     "stability check. In-sample evidence only; the pre-registered holdout "
                     "test (c) awaits approval.**")
    lines += ["", ("Next step (c), only after approval: "
                   f"`python experiments/plateau_hunt.py --holdout --center "
                   f"{' '.join(f'{k}={v}' for k, v in combo.items())}` — holdout "
                   f"\"{HOLDOUT_STATUS}\"."), "", "Not financial advice."]
    return "\n".join(lines)


def grid_table(res: dict, cfg) -> pd.DataFrame:
    ann = _ann(cfg)
    full = res["full"]
    df = pd.DataFrame(res["run"]["cells"])
    for s in full["per_symbol"]:
        df[f"sharpe_{s}"] = full["per_symbol"][s] * ann
        df[f"trades_{s}"] = full["trades"][s]
    df["score"] = full["scores"] * ann
    df["neighbourhood"] = full["neigh"] * ann
    df["eligible"] = full["eligible"]
    for i, h in enumerate(res.get("halves", [])):
        df[f"h{i + 1}_score"] = h["scores"] * ann
        df[f"h{i + 1}_neighbourhood"] = h["neigh"] * ann
        df[f"h{i + 1}_eligible"] = h["eligible"]
    df["selected"] = [i == res["center"] for i in range(len(df))]
    return df


def render_holdout(out: dict, combo: dict, cfg) -> str:
    ann = _ann(cfg)
    blk = out["block"]
    lines = ["# Plateau hunt (c): sealed-holdout shot", "",
             f"- Configuration (fixed, selected in reports/plateau_hunt.md): **{_combo_str(combo)}**",
             f"- Holdout [{HOLDOUT[0]}, {HOLDOUT[1]}) - status: \"{HOLDOUT_STATUS}\"",
             f"- Code state at run time: {_git_state()}", "",
             "Per symbol: " + "; ".join(f"{s} Sharpe {sh * ann:+.2f} ({n} trades)"
                                        for s, (sh, n) in out["per_symbol"].items()), ""]
    lines += validate.format_block("Pooled equal-weight holdout", blk)
    lines += ["## Verdict", "",
              ("**Confirmed on the holdout (pre-registered test passed).**" if not blk["noise"]
               else f"**{validate.NOISE_SENTENCE}** The holdout does not confirm the selection."),
              "", "Not financial advice."]
    return "\n".join(lines)


# --------------------------------------------------------------------- main

def _parse_center(items: list[str]) -> dict:
    combo = {}
    for item in items:
        k, _, v = item.partition("=")
        if k not in GRID or float(v) not in GRID[k]:
            raise SystemExit(f"--center {item!r}: not a cell of the pre-registered grid")
        combo[k] = float(v)
    if set(combo) != set(GRID):
        raise SystemExit(f"--center needs every knob: {', '.join(GRID)}")
    return {k: combo[k] for k in GRID}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pre-registered plateau hunt (see module doc)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--holdout", action="store_true",
                    help="step (c): the one holdout shot - only after approval")
    ap.add_argument("--center", nargs="+", default=None, metavar="KNOB=VALUE")
    ap.add_argument("--out-dir", default="reports")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    v = cfg.validation
    full_bars = load_bars(args.data_dir or cfg.data.dir, cfg)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.holdout:
        if not args.center:
            raise SystemExit("--holdout needs --center (the cell selected in step (a))")
        combo = _parse_center(args.center)
        out = holdout(full_bars, cfg, combo, n_boot=v.bootstrap_reps,
                      block=v.bootstrap_block_bars)
        text = render_holdout(out, combo, cfg)
        (out_dir / "plateau_holdout.md").write_text(text, encoding="utf-8")
        print("\n".join(text.splitlines()[-4:]))
        return 0

    bars = {s: search_slice(b) for s, b in full_bars.items()}
    print(f"Plateau hunt: {len(cells(GRID))} cells x {len(bars)} symbols on 2021-2024 ...")
    res = hunt(bars, cfg, n_boot=v.bootstrap_reps, block=v.bootstrap_block_bars)
    text = render(res, cfg)
    (out_dir / "plateau_hunt.md").write_text(text, encoding="utf-8")
    grid_table(res, cfg).to_csv(out_dir / "plateau_hunt.csv", index=False)
    verdict = text[text.index("## Verdict"):] if "## Verdict" in text else text
    print(verdict)
    print(f"\nFull report: {out_dir / 'plateau_hunt.md'} (+ plateau_hunt.csv, all cells)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Synthetic-regime sensitivity sweep: what is a regime call *worth*?

The human layer (docs/DESIGN.md §0) only pays if the human's regime-call
accuracy beats a break-even that depends on how much the engine makes when
armed in a good regime versus loses when armed in a bad one. This sweep maps
that value-of-accuracy curve with synthetic humans, before any real human's
track record exists (DESIGN.md §7.4):

1. Run the real walk-forward engine over the window and label every traded
   OOS test window *good* or *bad* by its realised engine P&L. The labels are
   forward-looking by construction — legitimate here, because the sweep never
   claims to predict anything; it measures plumbing and value-at-accuracy.
2. For each accuracy p, draw M seeded synthetic humans: before each window,
   arm with probability p if the window is good, disarm with probability p if
   it is bad. Each synthetic human is one ``armed`` schedule.
3. Run the full system (engine + risk overlay) under every schedule and
   report total return, max drawdown and per-trade expectancy against p, with
   the dispersion across the M humans.
4. Fit the empirical break-even accuracy and set it beside the arithmetic on
   the same windows, the design's L/(W+L) formula on measured W and L, the
   theoretical 40% and the brief's 56%. Agreement validates the overlay
   plumbing end to end; disagreement is a bug in the plumbing or the
   arithmetic, and either way it is found here, not in production.

How the M x |p| schedules stay cheap without cutting corners: the walk-forward
selection is fixed (the human never changes which parameters the engine
trades), and the overlay state restarts at each OOS window — the same
convention as ``walkforward.run(overlay=...)``. A window's outcome then
depends only on its own call and the previous one (continuing armed, freshly
armed — transition cooldown — or disarmed), so each window is replayed
through ``backtest.run`` once per case and every schedule is assembled
exactly from those replays.

The caveat is printed with every report and is the point, not fine print:
synthetic labels hand the human a clairvoyant-grade *information basis*. The
sweep prices accuracy; it does not certify that any real human achieves
p = 0.7. Only the journal (``near-bot journal report``) measures that.

    python experiments/regime_sweep.py --csv data/NEARUSDT_1m_20230101_20260101.csv
"""

from __future__ import annotations

import argparse
import copy
import math
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
import pandas as pd

from near_bot import backtest as bt
from near_bot import data, indicators, stats, validate
from near_bot import score as score_mod
from near_bot import walkforward as wf
from near_bot.config import Config

STEADY, FRESH, OFF = "armed", "armed_fresh", "disarmed"
THEORETICAL_BREAK_EVEN = 0.40   # DESIGN.md §0: W = +0.9%, L = -0.6%
BRIEF_BREAK_EVEN = 0.56         # the project brief's figure, shown for reference
PLUMBING_TOLERANCE = 0.05       # |empirical - arithmetic| break-even agreement
THIN_WINDOWS = 30               # fewer labelled windows than this is thin


@dataclass
class WindowReplay:
    """One traded OOS window: its label and the full system's three cases."""

    test_range: tuple[int, int]
    engine_return: float            # engine-only OOS return (defines the label)
    n_legs: int                     # ensemble legs traded in this window
    variants: dict[str, tuple[np.ndarray, list]] = field(default_factory=dict)

    @property
    def good(self) -> bool:
        return self.engine_return > 0.0


# ------------------------------------------------------------------ replays

def replay_windows(bars: pd.DataFrame, cfg, res: wf.WalkForwardResult) -> list[WindowReplay]:
    """Replay every traded OOS window through engine + overlay, once per case.

    Uses the combos the walk-forward selected for that window; each leg gets
    its own overlay instance (fresh per window), and the legs' per-bar
    returns are averaged exactly as the walk-forward ensemble does.
    """
    cache: dict[tuple, tuple] = {}

    def frame_for(combo: dict):
        key = tuple(sorted(combo.items()))
        if key not in cache:
            params = replace(cfg.strategy, **combo)
            frame = indicators.add_all(bars, params)
            cache[key] = (frame, score_mod.compute(frame, params), params)
        return cache[key]

    cases = ((STEADY, True, True), (FRESH, True, False), (OFF, False, True))
    out = []
    for w in res.windows:
        if w.combos is None:
            continue
        a, b = w.test_range
        rep = WindowReplay(w.test_range, w.out_metrics.get("total_return", 0.0),
                           len(w.combos))
        for name, armed, before in cases:
            legs, trades = [], []
            for combo in w.combos:
                frame, score, params = frame_for(combo)
                sub = copy.copy(cfg)
                sub.strategy = params
                r = bt.run(frame.iloc[a:b], sub, score.iloc[a:b], overlay=cfg.overlay,
                           armed=pd.Series(armed, index=range(b - a)),
                           armed_before=before)
                legs.append(r.bar_returns.to_numpy())
                trades.extend(r.trades)
            rep.variants[name] = (np.mean(legs, axis=0), trades)
        out.append(rep)
    return out


# ---------------------------------------------------------------- schedules

def make_schedule(good: np.ndarray, p: float, rng: np.random.Generator) -> np.ndarray:
    """One synthetic human: the right call (arm good / disarm bad) w.p. p."""
    good = np.asarray(good, dtype=bool)
    correct = rng.random(len(good)) < p
    return np.where(correct, good, ~good)


def assemble(replays: list[WindowReplay], schedule, armed_before: bool = False) -> dict:
    """The full system's path under one schedule, from the cached replays.

    A fresh deployment starts DISARMED (``armed_before=False``), so arming
    the first window is a transition and pays the cooldown.
    """
    parts, pnls = [], []
    prev = armed_before
    for rep, armed in zip(replays, schedule, strict=True):
        case = OFF if not armed else (STEADY if prev else FRESH)
        rets, trades = rep.variants[case]
        parts.append(rets)
        pnls.extend(t.weighted_net / rep.n_legs for t in trades)
        prev = bool(armed)
    r = np.concatenate(parts) if parts else np.zeros(0)
    return {
        "total_return": stats.total_return_stat(r),
        "max_drawdown": stats.max_drawdown_stat(r),
        "sum_return": float(r.sum()),
        "expectancy": float(np.mean(pnls)) if pnls else math.nan,
        "n_trades": len(pnls),
        "armed_share": float(np.mean(schedule)) if len(schedule) else 0.0,
    }


def _summarise(rows: list[dict]) -> dict:
    out = {}
    for key in rows[0]:
        x = np.array([r[key] for r in rows], dtype=float)
        x = x[np.isfinite(x)]
        out[key] = ({"mean": float(x.mean()), "p5": float(np.percentile(x, 5)),
                     "p95": float(np.percentile(x, 95))} if len(x)
                    else {"mean": math.nan, "p5": math.nan, "p95": math.nan})
    return out


def sweep(replays: list[WindowReplay], accuracies, n_schedules: int, seed: int = 7) -> dict:
    """Summaries per accuracy (mean and 5th/95th percentile across humans),
    plus the deterministic oracle bounds p = 0 (always wrong) and p = 1."""
    good = np.array([r.good for r in replays])
    out = {}
    for p in accuracies:
        rng = np.random.default_rng([seed, round(p * 1000)])
        out[p] = _summarise([assemble(replays, make_schedule(good, p, rng))
                             for _ in range(n_schedules)])
    out["oracle"] = {
        1.0: _summarise([assemble(replays, good)]),
        0.0: _summarise([assemble(replays, ~good)]),
    }
    return out


# --------------------------------------------------------------- break-even

def break_evens(replays: list[WindowReplay], swept: dict) -> dict:
    """Empirical break-even vs the arithmetic on the same windows.

    * empirical: least-squares line through the mean summed return per p
      (the expectation is linear in p), solved for zero;
    * arithmetic: |S_bad| / (S_good + |S_bad|) with S = summed full-system
      returns of the continuing-armed case over good / bad windows — the
      same quantity the empirical fit estimates, minus cooldown effects;
    * design formula: L / (W + L) on measured per-trade W (armed, good
      windows) and L (armed, bad windows).
    """
    accs = sorted(p for p in swept if p != "oracle")
    out = {"empirical": math.nan, "arithmetic": math.nan, "design_formula": math.nan,
           "W": math.nan, "L": math.nan}
    if len(accs) >= 2:
        slope, intercept = np.polyfit(accs, [swept[p]["sum_return"]["mean"] for p in accs], 1)
        if slope > 0:
            out["empirical"] = float(-intercept / slope)
    s_good = sum(float(r.variants[STEADY][0].sum()) for r in replays if r.good)
    s_bad = sum(float(r.variants[STEADY][0].sum()) for r in replays if not r.good)
    if s_good > 0 > s_bad:
        out["arithmetic"] = -s_bad / (s_good - s_bad)
    wins = [t.weighted_net / r.n_legs for r in replays if r.good for t in r.variants[STEADY][1]]
    losses = [t.weighted_net / r.n_legs for r in replays if not r.good
              for t in r.variants[STEADY][1]]
    if wins and losses:
        w, loss = float(np.mean(wins)), -float(np.mean(losses))
        out["W"], out["L"] = w, -loss
        if w > 0 and loss > 0:
            out["design_formula"] = loss / (w + loss)
    return out


# ------------------------------------------------------------------- report

def _pct(x: float, fmt: str = "+.2%") -> str:
    return "n/a" if not np.isfinite(x) else f"{x:{fmt}}"


def _band(d: dict, fmt: str = "+.2%") -> str:
    return f"{_pct(d['mean'], fmt)} [{_pct(d['p5'], fmt)}, {_pct(d['p95'], fmt)}]"


def verdict_lines(replays: list[WindowReplay], swept: dict, be: dict) -> list[str]:
    """The mandatory verdict (DESIGN.md §7.6), plumbing check first."""
    lines = ["## Verdict", ""]
    emp, ari = be["empirical"], be["arithmetic"]
    if np.isfinite(emp) and np.isfinite(ari):
        if abs(emp - ari) <= PLUMBING_TOLERANCE:
            lines.append(f"- Plumbing: consistent - the sweep's break-even {emp:.0%} matches "
                         f"the arithmetic on the same windows {ari:.0%} (within "
                         f"{PLUMBING_TOLERANCE:.0%}; the residual is cooldown + sampling).")
        else:
            lines.append(f"- Plumbing: DISAGREEMENT - sweep {emp:.0%} vs arithmetic {ari:.0%}. "
                         "A bug in the schedule -> overlay -> accounting path, or in the "
                         "arithmetic: investigate before trusting any number here.")
    else:
        lines.append("- Plumbing: cannot be checked - the engine has no good/bad contrast "
                     "on these windows (a break-even needs both winning and losing regimes).")
    accs = sorted(p for p in swept if p != "oracle")
    for p in accs:
        d = swept[p]["total_return"]
        clear = np.isfinite(d["p5"]) and d["p5"] > 0.0
        lines.append(f"- p = {p:.0%}: total return {_band(d)} across synthetic humans -> "
                     + ("positive for 95% of them" if clear else
                        "band includes zero: not distinguishable from noise"))
    lines.append("")
    reliable = [p for p in accs if swept[p]["total_return"]["p5"] > 0.0]
    if not accs or accs[-1] not in reliable:
        overall = f"**Overall: {validate.NOISE_SENTENCE}**"
        if accs:
            overall += (f" Even at {accs[-1]:.0%} accuracy the system's result is not "
                        "reliably positive on these windows.")
        lines.append(overall)
    else:
        first = reliable[0]
        lines.append(f"**Overall: a regime call accurate to >= {first:.0%} makes this "
                     "system reliably positive on these windows. Whether a real human "
                     "gets there is measured only by the journal.**")
    n = len(replays)
    if n < THIN_WINDOWS:
        lines.append(f"- {n} labelled windows < {THIN_WINDOWS}: each window is one "
                     "independent unit here, so this curve is statistically thin.")
    lines += [
        "",
        ("Caveat: synthetic labels give the human a clairvoyant-grade information "
         "basis. This sweep maps the value of accuracy; it does not certify that a "
         "real human achieves any of these accuracies. Only the journal "
         "(`near-bot journal report`) measures that."),
    ]
    return lines


def render(replays: list[WindowReplay], swept: dict, be: dict, header: list[str]) -> str:
    n_good = sum(r.good for r in replays)
    n_flat = sum(1 for r in replays if r.engine_return == 0.0)
    lines = ["# Synthetic-regime sensitivity sweep (DESIGN.md §7.4)", "", *header,
             (f"- Labelled windows: {len(replays)} traded ({n_good} good, "
              f"{len(replays) - n_good} bad, of which {n_flat} flat); windows the "
              "walk-forward skipped carry no trades and are left out"), "",
             "## Value of accuracy", "",
             ("Mean across synthetic humans, [5th, 95th] percentile in brackets. "
              "Rows 0% and 100% are the deterministic oracle bounds."), "",
             "| p | total return | max drawdown | expectancy / trade | trades | armed |",
             "|---|---|---|---|---|---|"]
    rows = [(0.0, swept["oracle"][0.0])]
    rows += [(p, swept[p]) for p in sorted(p for p in swept if p != "oracle")]
    rows += [(1.0, swept["oracle"][1.0])]
    for p, s in rows:
        lines.append(f"| {p:.0%} | {_band(s['total_return'])} | "
                     f"{_band(s['max_drawdown'], '.2%')} | "
                     f"{_pct(s['expectancy']['mean'], '+.3%')} | "
                     f"{s['n_trades']['mean']:.1f} | {s['armed_share']['mean']:.0%} |")
    lines += [
        "", "## Break-even accuracy", "",
        f"- Empirical (line fit through the sweep's mean summed return): {_pct(be['empirical'], '.0%')}",
        f"- Arithmetic on the same windows, |S_bad| / (S_good + |S_bad|): {_pct(be['arithmetic'], '.0%')}",
        (f"- Design formula L / (W + L) on measured per-trade W = {_pct(be['W'], '+.3%')}, "
         f"L = {_pct(be['L'], '+.3%')}: {_pct(be['design_formula'], '.0%')}"),
        f"- Theoretical (DESIGN.md §0, W = +0.9%, L = -0.6%): {THEORETICAL_BREAK_EVEN:.0%}",
        f"- The brief's figure (reference only): {BRIEF_BREAK_EVEN:.0%}",
        "",
    ]
    lines += verdict_lines(replays, swept, be)
    lines += ["", "Not financial advice."]
    return "\n".join(lines)


# --------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--csv", default=None,
                    help="1m klines CSV (default: newest dated window for the config symbol)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--train-days", type=int, default=None,
                    help="default: validation.train_days")
    ap.add_argument("--test-days", type=int, default=None,
                    help="default: validation.test_days")
    ap.add_argument("--top-k", type=int, default=None, help="default: validation.top_k")
    ap.add_argument("--accuracies", type=float, nargs="+", default=None,
                    help="default: validation.synthetic_accuracies")
    ap.add_argument("--schedules", type=int, default=None,
                    help="synthetic humans per accuracy (default: validation.synthetic_schedules)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="reports/regime_sweep.md")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    v = cfg.validation
    csv = args.csv
    if csv is None:
        _, found, _ = validate.locate_csvs([cfg.symbol], cfg.data.dir,
                                           cfg.data.start, cfg.data.end)
        if not found:
            print(f"No dated-window CSV for {cfg.symbol} in {cfg.data.dir}; download one "
                  f"first (scripts/download_data.py) or pass --csv.", file=sys.stderr)
            return 1
        csv = str(found[cfg.symbol])
    train_d = args.train_days or v.train_days
    test_d = args.test_days or v.test_days
    top_k = args.top_k or v.top_k
    accuracies = args.accuracies or v.synthetic_accuracies
    n_sched = args.schedules or v.synthetic_schedules
    bars_per_day = 1440 // cfg.aggregate_minutes

    bars = data.aggregate(data.load_csv(csv), cfg.aggregate_minutes, drop_incomplete=True)
    print(f"Walk-forward (engine only, for the labels): train {train_d}d / test {test_d}d ...")
    res = wf.run(bars, cfg, train_bars=train_d * bars_per_day,
                 test_bars=test_d * bars_per_day, top_k=top_k)
    replays = replay_windows(bars, cfg, res)
    if not replays:
        print("No traded OOS windows: nothing to label, nothing to sweep.", file=sys.stderr)
        return 1
    print(f"Replayed {len(replays)} windows x 3 cases through engine + overlay; "
          f"sweeping {len(accuracies)} accuracies x {n_sched} synthetic humans ...")
    swept = sweep(replays, accuracies, n_sched, seed=args.seed)
    be = break_evens(replays, swept)
    header = [
        (f"- Data: `{csv}`, {cfg.symbol} {cfg.aggregate_minutes}m bars; walk-forward "
         f"train {train_d}d / test {test_d}d, top-{top_k} ensemble (selection fixed "
         "from the engine-only run)"),
        (f"- Synthetic humans: {n_sched} per accuracy, seed {args.seed}; full system = "
         "engine + risk overlay (kill switch, risk sizing, transition cooldown), overlay "
         "state restarted per window; a deployment starts DISARMED"),
    ]
    text = render(replays, swept, be, header)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print()
    print("\n".join(verdict_lines(replays, swept, be)))
    print(f"\nFull report (value-of-accuracy table, break-evens): {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The journal: an append-only record of every permission decision.

Why append-only
---------------
The journal is the measurement instrument for the human's regime-call
accuracy — the 60–80% assumption is tested against this file, not trusted.
That makes its integrity more important than its convenience: there is no
edit command, a mistake is corrected by appending a ``correction`` event,
and even no-op decisions (arming while armed) are recorded, because the
journal measures what the human *did*, not what changed.

Scoring (``near-bot journal report``)
-------------------------------------
Each arm/disarm call is scored against the subsequent N-day window, per call
type. The primary outcome is the *strategy's* replayed net P&L over the
window — the human permissions this engine, so the honest question is
whether the engine made money while armed and stayed out of trouble while
disarmed. The secondary outcome is the forward buy-and-hold return, which
prices missed trends (a disarm before a +15% week is wrong even if the
engine would have been gated off anyway). Both accuracies get bootstrap
confidence intervals and are compared against the break-even implied by
measured win/loss sizes — never against an asserted number.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

COLUMNS = ["ts_utc", "event", "state_after", "note", "equity", "price", "hwm", "context"]

HUMAN_EVENTS = ("arm", "disarm")
AUTO_EVENTS = ("auto_halt_day", "auto_half_on", "auto_half_off", "auto_full_stop",
               "auto_flatten", "cooldown_done", "correction", "trade")


def append(path: str | Path, event: str, state_after: str, note: str,
           equity: float | str = "", price: float | str = "",
           hwm: float | str = "", context: str = "") -> None:
    """Append one event. Creates the file (with header) on first use."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(COLUMNS)
        w.writerow([dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    event, state_after, note, equity, price, hwm, context])


def load(path: str | Path) -> pd.DataFrame:
    """Load the journal; a missing file returns an empty, correctly-typed frame."""
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, dtype=str).fillna("")
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df


def _trade_pnls(events: pd.DataFrame) -> np.ndarray:
    """Realised weighted P&L of journaled trades (context='weighted_pct=...')."""
    out = []
    for ctx in events.loc[events["event"] == "trade", "context"]:
        for kv in str(ctx).split():
            if kv.startswith("weighted_pct="):
                try:
                    out.append(float(kv.split("=", 1)[1]))
                except ValueError:
                    pass
    return np.array(out)


def measured_break_even(events: pd.DataFrame) -> float | None:
    """Empirical break-even regime accuracy p* = L / (W + L) from journaled trades.

    With per-trade expectancy +W when the regime call is right and −L when
    wrong, the call must be right with probability L/(W+L) to break even.
    Returns None while the journal lacks winning or losing trades.
    """
    pnls = _trade_pnls(events)
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    if len(wins) == 0 or len(losses) == 0:
        return None
    w, l = float(wins.mean()), abs(float(losses.mean()))
    return l / (w + l)


def score_calls(events: pd.DataFrame, frame: pd.DataFrame, cfg,
                horizon_days: int = 5) -> pd.DataFrame:
    """Score every arm/disarm call against its forward ``horizon_days`` window.

    ``frame`` is the indicator frame (``indicators.add_all`` output) covering
    the journal's time span. For each call the engine is replayed over the
    forward window; indicators are causal and computed on the full frame, so
    slicing leaks nothing. Windows running past the data are marked ``open``.
    """
    from . import backtest as bt  # local import: keeps journal importable standalone

    calls = events[events["event"].isin(HUMAN_EVENTS)].reset_index(drop=True)
    rows = []
    if frame.empty or calls.empty:
        return pd.DataFrame(rows)
    times = frame["timestamp"].to_numpy()
    closes = frame["close"].to_numpy(dtype=float)
    horizon = np.timedelta64(horizon_days, "D")

    for _, call in calls.iterrows():
        t0 = call["ts_utc"].to_datetime64()
        a = int(np.searchsorted(times, t0))
        b = int(np.searchsorted(times, t0 + horizon))
        row = {"ts_utc": call["ts_utc"], "event": call["event"],
               "note": call["note"], "n_bars": max(0, b - a)}
        if a >= len(times) - 1 or b - a < 2 or b > len(times):
            row.update(status="open", strat_ret=np.nan, market_ret=np.nan,
                       correct=np.nan)
        else:
            res = bt.run(frame.iloc[a:b], cfg)
            strat = res.metrics.get("total_return", 0.0)
            market = closes[b - 1] / closes[a] - 1.0
            correct = strat > 0.0 if call["event"] == "arm" else strat <= 0.0
            row.update(status="scored", strat_ret=strat, market_ret=market,
                       correct=bool(correct))
        rows.append(row)
    return pd.DataFrame(rows)


def accuracy_report(scored: pd.DataFrame, events: pd.DataFrame,
                    n_boot: int = 10000, seed: int = 7) -> dict:
    """Accuracy per call type with bootstrap CIs, against the measured break-even.

    Two bootstrap flavours: plain (resample calls) and streak (block bootstrap
    over the chronological call sequence, block 10) — consecutive calls are
    not independent, and the streak CI is the honest one of the two.
    """
    from . import stats

    out: dict = {"n_calls": len(scored), "n_scored": 0, "by_type": {}}
    done = scored[scored["status"] == "scored"] if len(scored) else scored
    out["n_scored"] = len(done)
    if not len(done):
        return out

    for event_type in HUMAN_EVENTS:
        sub = done[done["event"] == event_type]
        if len(sub):
            pt, lo, hi = stats.bootstrap_ci(
                sub["correct"].to_numpy(dtype=float), stats.mean_stat,
                n_boot=n_boot, block=max(1, min(5, len(sub))), seed=seed)
            out["by_type"][event_type] = {
                "n": len(sub), "accuracy": float(sub["correct"].mean()),
                "ci": (lo, hi),
                "avg_strat_ret": float(sub["strat_ret"].mean()),
                "avg_market_ret": float(sub["market_ret"].mean()),
            }
    correct = done["correct"].to_numpy(dtype=float)
    pt, lo, hi = stats.bootstrap_ci(correct, stats.mean_stat, n_boot=n_boot,
                                    block=max(1, min(5, len(done))), seed=seed)
    _, slo, shi = stats.bootstrap_ci(correct, stats.mean_stat, n_boot=n_boot,
                                     block=max(1, min(10, len(done))), seed=seed + 1)
    out["overall"] = {"n": len(done), "accuracy": pt, "ci": (lo, hi),
                      "ci_streak": (slo, shi)}
    out["break_even"] = measured_break_even(events)

    disarmed = done[done["event"] == "disarm"]
    out["missed_move_cost"] = float(disarmed["market_ret"].sum()) if len(disarmed) else 0.0

    be = out["break_even"]
    if be is not None:
        lo_s, hi_s = out["overall"]["ci_streak"]
        if lo_s > be:
            verdict = (f"measured accuracy {pt:.0%} [{lo_s:.0%}-{hi_s:.0%}] "
                       f"> break-even {be:.0%} -> distinguishable")
        elif hi_s < be:
            verdict = (f"measured accuracy {pt:.0%} [{lo_s:.0%}-{hi_s:.0%}] "
                       f"< break-even {be:.0%} -> the human layer destroys value")
        else:
            verdict = (f"measured accuracy {pt:.0%} [{lo_s:.0%}-{hi_s:.0%}] vs "
                       f"break-even {be:.0%} -> NOT distinguishable at this sample size")
        out["verdict"] = verdict
    return out


def format_report(scored: pd.DataFrame, rep: dict) -> str:
    """Render the journal report as plain text for the CLI."""
    lines = [(f"Journal: {rep['n_calls']} human calls, {rep['n_scored']} scored "
              f"(rest open or beyond available data)")]
    for t, d in rep.get("by_type", {}).items():
        lo, hi = d["ci"]
        lines.append(f"  {t:<7} n={d['n']:<3} accuracy {d['accuracy']:.0%} "
                     f"[{lo:.0%}-{hi:.0%}]  avg strat {d['avg_strat_ret']:+.2%}  "
                     f"avg market {d['avg_market_ret']:+.2%}")
    if "overall" in rep:
        o = rep["overall"]
        lo, hi = o["ci"]
        slo, shi = o["ci_streak"]
        lines.append(f"  overall n={o['n']:<3} accuracy {o['accuracy']:.0%} "
                     f"[{lo:.0%}-{hi:.0%}] (streak-robust [{slo:.0%}-{shi:.0%}])")
    if rep.get("break_even") is not None:
        lines.append(f"Measured break-even accuracy (from journaled trades): "
                     f"{rep['break_even']:.0%}")
    else:
        lines.append("Measured break-even accuracy: n/a (need winning and losing "
                     "journaled trades; theoretical p* = L/(W+L))")
    lines.append(f"Missed-move cost of disarmed windows (sum of market returns): "
                 f"{rep.get('missed_move_cost', 0.0):+.2%}")
    if "verdict" in rep:
        lines.append(f"VERDICT: {rep['verdict']}")
    return "\n".join(lines)

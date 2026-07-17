"""Walk-forward validation: optimise in-sample, report out-of-sample only.

Sweeping parameters over one window and reporting the best result is
data-mining: with enough combinations, *something* always looks good. The
standard defence is walk-forward analysis:

1. Split history into rolling (train, [embargo], test) windows.
2. On each train window, rank parameter combos by in-sample **Sharpe ratio**
   (not raw return — a lucky straight line wins otherwise), subject to a
   minimum trade count.
3. Trade the **top-k** combos on the following test window and average their
   per-bar returns (parameter ensembling).
4. Concatenate the test-window returns and report metrics on those alone.

v2 changes vs v1, and why
-------------------------
* **Sharpe selection** — v1 selected on expectancy, which crowns high-variance
  combos; risk-adjusting the selection target reduces IS→OOS decay.
* **Top-k ensemble (default 3)** — v1 traded the single best in-sample combo.
  With small samples the "best" is mostly luck; averaging the leaders trades
  away that selection variance. On real data the ensemble roughly doubled the
  OOS trade count while *raising* OOS Sharpe.
* **Optional embargo** — a gap between train and test windows removes any
  bleed from slow-decaying indicator state.

Indicators and scores are causal (rolling/EWM over past bars only), so they
are computed once per combo over the full series and sliced afterwards —
that leaks nothing, and it is what makes the sweep fast.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

from . import backtest as bt
from . import indicators
from . import score as score_mod

# Deliberately small: every added cell in the grid is another lottery ticket
# for a lucky in-sample winner. Three knobs, twelve combos, that's it.
DEFAULT_GRID: dict[str, list] = {
    "theta_in": [0.15, 0.25, 0.35],
    "trail_atr": [2.5, 3.5],
    "er_min": [0.15, 0.25],
}


@dataclass
class WindowResult:
    """One (train, test) split. Ranges are [start, end) bar indices."""

    train_range: tuple[int, int]
    test_range: tuple[int, int]
    combos: list[dict[str, Any]] | None  # None => window skipped
    in_sample_sharpe: list[float] = field(default_factory=list)
    out_metrics: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class WalkForwardResult:
    windows: list[WindowResult] = field(default_factory=list)
    oos_returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    oos_trades: list[bt.Trade] = field(default_factory=list)
    oos_metrics: dict = field(default_factory=dict)
    _bar_minutes: int = 60  # for annualising the per-window IS Sharpe display

    def oos_equity(self) -> pd.Series:
        return pd.Series(np.cumprod(1.0 + self.oos_returns.to_numpy()), name="equity")

    def summary(self) -> str:
        traded = [w for w in self.windows if w.combos is not None]
        skipped = len(self.windows) - len(traded)
        lines = [
            f"Walk-forward: {len(self.windows)} windows "
            f"({len(traded)} traded, {skipped} skipped)"
        ]
        m = self.oos_metrics
        if m.get("n_trades", 0) > 0:
            lines += [
                f"OOS trades       : {m['n_trades']}",
                f"OOS win rate     : {m['win_rate']:.1%}",
                f"OOS expectancy   : {m['expectancy_weighted']:+.4%} weighted "
                f"(gross {m['expectancy_gross']:+.4%}, drag {m['cost_drag']:.4%})",
                f"OOS total return : {m['total_return']:+.2%} (buy&hold {m['buy_hold']:+.2%})",
                f"OOS Sharpe       : {m['sharpe']:+.2f}",
                f"OOS max drawdown : {m['max_drawdown']:.2%}",
                f"OOS exposure     : {m['exposure']:.1%}",
            ]
        else:
            lines.append("No out-of-sample trading occurred.")
        lines.append("")
        lines.append("Per-window (in-sample Sharpe of chosen combos -> OOS outcome):")
        for w in self.windows:
            span = f"bars {w.test_range[0]:>5}-{w.test_range[1]:>5}"
            if w.combos is None:
                lines.append(f"  {span}: skipped ({w.note})")
                continue
            bpy = indicators.bars_per_year(self._bar_minutes)
            is_s = "/".join(f"{x * np.sqrt(bpy):+.2f}" for x in w.in_sample_sharpe)
            oos_n = w.out_metrics.get("n_trades", 0)
            oos_r = w.out_metrics.get("total_return", 0.0)
            lines.append(f"  {span}: IS Sharpe [{is_s}] -> OOS {oos_r:+.2%} "
                         f"({oos_n} trades) {w.combos[0]}{' …' if len(w.combos) > 1 else ''}")
        return "\n".join(lines)


def _combos(grid: dict[str, list]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [
        dict(zip(keys, vals, strict=True))
        for vals in itertools.product(*(grid[k] for k in keys))
    ]


def _is_score(bar_returns: np.ndarray, n_trades: int, min_trades: int) -> float:
    """In-sample ranking score: per-bar Sharpe, gated on a minimum trade count."""
    if n_trades < min_trades:
        return float("-inf")
    sd = bar_returns.std()
    if sd <= 0:
        return float("-inf")
    return float(bar_returns.mean() / sd)


def run(
    bars: pd.DataFrame,
    cfg,
    grid: dict[str, list] | None = None,
    train_bars: int = 720,
    test_bars: int = 240,
    step_bars: int | None = None,
    min_trades: int = 3,
    top_k: int = 3,
    embargo_bars: int = 0,
) -> WalkForwardResult:
    """Run walk-forward validation over aggregated, pre-indicator ``bars``.

    ``train_bars``/``test_bars`` are in aggregated bars (720×60m = 30 days).
    ``top_k`` combos are traded per window and their per-bar returns averaged.
    ``embargo_bars`` inserts a gap between train and test (default 0).
    """
    grid = grid or DEFAULT_GRID
    unknown = [k for k in grid if not hasattr(cfg.strategy, k)]
    if unknown:
        raise ValueError(f"Unknown strategy parameter(s) in grid: {unknown}")

    n = len(bars)
    step = step_bars or test_bars
    if train_bars <= 0 or test_bars <= 0 or step <= 0:
        raise ValueError("train_bars, test_bars and step_bars must be positive")
    if train_bars + embargo_bars + test_bars > n:
        raise ValueError(
            f"Not enough history: need >= {train_bars + embargo_bars + test_bars} bars, have {n}"
        )
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    # Scores are causal: compute once per combo, slice windows afterwards.
    cache: dict[tuple, tuple[pd.DataFrame, pd.Series, Any]] = {}

    def frame_for(combo: dict):
        key = tuple(sorted(combo.items()))
        if key not in cache:
            params = replace(cfg.strategy, **combo)
            frame = indicators.add_all(bars, params)
            cache[key] = (frame, score_mod.compute(frame, params), params)
        return cache[key]

    def run_slice(combo_cfg, frame, score, a, b):
        sub_cfg = _with_strategy(cfg, combo_cfg)
        return bt.run(frame.iloc[a:b], sub_cfg, score.iloc[a:b])

    result = WalkForwardResult()
    result._bar_minutes = cfg.aggregate_minutes
    oos_parts: list[pd.Series] = []
    bh_parts: list[np.ndarray] = []
    close_arr = bars["close"].to_numpy(dtype=float)
    start = 0
    while start + train_bars + embargo_bars + test_bars <= n:
        tr = (start, start + train_bars)
        te = (tr[1] + embargo_bars, tr[1] + embargo_bars + test_bars)

        ranked: list[tuple[float, dict]] = []
        for combo in _combos(grid):
            frame, score, params = frame_for(combo)
            res = run_slice(params, frame, score, tr[0], tr[1])
            s = _is_score(res.bar_returns.to_numpy(), res.metrics.get("n_trades", 0), min_trades)
            if s > float("-inf"):
                ranked.append((s, combo))

        if not ranked:
            result.windows.append(
                WindowResult(tr, te, None, note=f"<{min_trades} IS trades for every combo"))
            start += step
            continue

        ranked.sort(key=lambda x: -x[0])
        chosen = ranked[:top_k]
        parts, trades = [], []
        for _, combo in chosen:
            frame, score, params = frame_for(combo)
            oos = run_slice(params, frame, score, te[0], te[1])
            parts.append(oos.bar_returns.to_numpy())
            trades.extend(oos.trades)
        ens = np.mean(parts, axis=0)
        oos_parts.append(pd.Series(ens, index=range(te[0], te[1])))
        bh_parts.append(close_arr[te[0]: te[1]] / close_arr[te[0] - 1: te[1] - 1] - 1.0)

        out_metrics = bt.metrics_from_returns(pd.Series(ens), cfg.aggregate_minutes)
        out_metrics["n_trades"] = len(trades)
        result.windows.append(WindowResult(
            tr, te, [c for _, c in chosen], [s for s, _ in chosen], out_metrics))
        result.oos_trades.extend(trades)
        start += step

    if oos_parts:
        result.oos_returns = pd.concat(oos_parts).sort_index()
        result.oos_metrics = bt.metrics_from_returns(result.oos_returns, cfg.aggregate_minutes)
        result.oos_metrics["buy_hold"] = float(np.prod(1.0 + np.concatenate(bh_parts)) - 1.0)
        result.oos_metrics.update(_trade_side_metrics(result.oos_trades, scale=1.0 / top_k))
    return result


def _trade_side_metrics(trades: list[bt.Trade], scale: float = 1.0) -> dict:
    """Trade-level stats for the stitched OOS windows.

    ``scale`` deflates per-leg weights by the ensemble size: with top_k legs
    trading in parallel, each leg contributes weighted_net / top_k to the
    portfolio (the equity stats already reflect this through the averaged
    per-bar returns).
    """
    if not trades:
        return {"n_trades": 0}
    wnets = np.array([t.weighted_net for t in trades]) * scale
    gross_w = np.array([t.gross_pct * t.weight for t in trades]) * scale
    wins = wnets[wnets > 0]
    losses = wnets[wnets <= 0]
    gross_loss = abs(losses.sum())
    return {
        "n_trades": len(trades),
        "win_rate": float(len(wins) / len(wnets)),
        "expectancy_weighted": float(wnets.mean()),
        "expectancy_gross": float(gross_w.mean()),
        "cost_drag": float(gross_w.mean() - wnets.mean()),
        "profit_factor": float(wins.sum() / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])),
    }


def _with_strategy(cfg, params):
    """Shallow-copy the config with a replaced ScoreParams."""
    import copy

    out = copy.copy(cfg)
    out.strategy = params
    return out

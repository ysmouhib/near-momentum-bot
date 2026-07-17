"""Plotting: equity vs buy&hold, drawdown, exposure and trade returns.

matplotlib is an *optional* dependency (``pip install "near-momentum-bot[plot]"``)
so the core library and test suite stay lightweight.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def _plt():
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless; we only write files
        import matplotlib.pyplot as plt

        return plt
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            'matplotlib is required for plots: pip install "near-momentum-bot[plot]"'
        ) from exc


def plot_equity(
    equity: pd.Series,
    metrics: dict,
    path: str | Path,
    title: str = "Backtest",
    buy_hold: pd.Series | None = None,
    position: pd.Series | None = None,
) -> Path:
    """Render equity (+ optional buy&hold), drawdown, and trade-return histogram."""
    plt = _plt()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0

    fig, axes = plt.subplots(
        3, 1, figsize=(9.5, 8.5), sharex=False,
        gridspec_kw={"height_ratios": [3, 1.1, 1.1]},
    )
    ax_eq, ax_dd, ax_hist = axes

    x = np.arange(len(equity))
    ax_eq.plot(x, equity.values, lw=1.7, color="#1f6f8b", label="strategy (net)")
    if buy_hold is not None and len(buy_hold) == len(equity):
        ax_eq.plot(x, buy_hold.values, lw=1.1, color="#9aa7ad", ls="--", label="buy & hold")
        ax_eq.legend(loc="upper left", fontsize=9)
    ax_eq.axhline(1.0, color="grey", lw=0.8, ls=":")
    ax_eq.set_ylabel("Equity (x initial)")
    subtitle = ""
    if metrics.get("n_trades"):
        subtitle = (
            f"{metrics['n_trades']} trades | win {metrics['win_rate']:.0%} | "
            f"Sharpe {metrics.get('sharpe', 0):+.2f} | "
            f"maxDD {metrics['max_drawdown']:.1%} | "
            f"exposure {metrics.get('exposure', 0):.0%}"
        )
    ax_eq.set_title(f"{title}\n{subtitle}", fontsize=11)

    ax_dd.fill_between(x, drawdown.values, 0, color="#c34a36", alpha=0.6)
    ax_dd.set_ylabel("Drawdown")
    if position is not None and len(position) == len(equity):
        ax2 = ax_dd.twinx()
        ax2.step(x, position.values, where="post", color="#4b8f8c", lw=0.8, alpha=0.7)
        ax2.set_ylabel("Weight", color="#4b8f8c")
        ax2.set_ylim(0, 1.05)
        ax2.tick_params(axis="y", labelcolor="#4b8f8c")
    ax_dd.set_xlabel("Bar #")

    rets = equity.pct_change().dropna()
    if len(rets):
        ax_hist.hist(rets.values * 100, bins=40, color="#4b8f8c", alpha=0.85)
        ax_hist.axvline(0, color="grey", lw=0.8, ls="--")
    ax_hist.set_xlabel("Per-bar net return (%)")
    ax_hist.set_ylabel("Count")

    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path

"""The v2 signal: a continuous conviction score in [-1, 1], not a boolean gate.

Why a score instead of v1's checklist
-------------------------------------
v1 AND-ed six binary conditions. That design has three failure modes:

1. **Brittle.** Nudge one threshold and individual trades flip on/off, so
   backtest performance jumps around discontinuously — the classic signature
   of a curve-fit system.
2. **No conviction axis.** "barely passed all gates" and "everything firing"
   were the same trade, so position sizing could not reflect signal strength.
3. **No hysteresis.** A signal hovering around a threshold re-triggers
   repeatedly, and at 0.24% round-trip costs churn is fatal.

The v2 score blends three *independent* families of evidence, each
volatility-normalised so they are comparable:

    score = w_mom * tanh-blend of TSMOM(fast, slow)     (trend persistence)
          + w_bo  * Donchian breakout position           (range expansion)
          + w_ofi * smoothed taker-imbalance z-score     (order flow)

Two regime gates multiply the tradability, not the score itself:

* **Efficiency-ratio gate** — Kaufman ER below ``er_min`` means chop; trend
  following in chop is paying fees to a random walk, so the score is zeroed.
* **Trend filter** — below the long SMA we simply do not go long. On
  90 days of real data this single filter moved a downtrending asset (SOL)
  from -5.9% to +0.3% out-of-sample: you cannot buy your way out of a
  downtrend, but you can decline to try.

Hysteresis (in the backtester, driven by ``theta_in`` > ``theta_out``) means
the position only changes when the score moves *meaningfully*, which is the
transaction-cost-aware way to trade a continuous signal: the no-trade band
around the threshold is where expected edge is smaller than round-trip cost.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

COMPONENTS = ("mom_fast", "mom_slow", "breakout", "ofi_z", "er", "trend_ma", "vol_bar", "atr")


def compute(frame: pd.DataFrame, p) -> pd.Series:
    """Composite conviction score for every bar of an indicator frame.

    ``frame`` must come from :func:`near_bot.indicators.add_all`. Returns a
    Series in [-1, 1]; NaN wherever any ingredient is not warmed up, and 0.0
    wherever a regime gate is closed (chop or downtrend).
    """
    mom = 0.5 * np.tanh(frame["mom_fast"]) + 0.5 * np.tanh(frame["mom_slow"])
    score = (
        p.w_mom * mom
        + p.w_bo * frame["breakout"]
        + p.w_ofi * (frame["ofi_z"] / 3.0)  # ofi_z is clipped to [-3, 3]
    ).clip(-1.0, 1.0)

    warm = frame[list(COMPONENTS)].notna().all(axis=1)
    trending = frame["close"] > frame["trend_ma"]
    efficient = frame["er"] >= p.er_min
    score = score.where(warm & trending & efficient, 0.0)
    score = score.where(warm, np.nan)  # still-warming bars stay NaN
    return score.rename("score")


def explain(row: pd.Series, score: float, p) -> str:
    """Human-readable one-liner for logs and trade records."""
    return (
        f"score={score:+.2f} mom={row['mom_fast']:+.2f}/{row['mom_slow']:+.2f} "
        f"bo={row['breakout']:+.2f} ofi={row['ofi_z']:+.2f} er={row['er']:.2f} "
        f"close={row['close']:.4f} trend_ma={row['trend_ma']:.4f}"
    )

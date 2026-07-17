"""Score behaviour: bounded, gated by regime, NaN only while warming up."""

from __future__ import annotations

import numpy as np
import pandas as pd

from near_bot import indicators as ind
from near_bot import score as score_mod
from near_bot.config import ScoreParams


def _frame(closes, seed=3):
    n = len(closes)
    rng = np.random.default_rng(seed)
    close = pd.Series(closes, dtype=float)
    df = pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.002,
        "low": close * 0.998,
        "close": close,
        "volume": rng.lognormal(9, 0.3, n),
        "taker_buy_volume": 0.0,
    })
    # noisy buy share ~ 0.6 (exactly-constant flow would zero the z-score's
    # dispersion, which the indicator correctly reports as undefined)
    buy_share = np.clip(0.6 + rng.normal(0, 0.03, n), 0.05, 0.95)
    df["taker_buy_volume"] = df["volume"] * buy_share
    return df


def test_score_bounded_and_nan_warmup():
    p = ScoreParams()
    closes = 3.0 * np.exp(np.cumsum(np.full(600, 0.002)))  # steady uptrend
    frame = ind.add_all(_frame(closes), p)
    s = score_mod.compute(frame, p)
    valid = s.dropna()
    assert valid.between(-1, 1).all()
    assert s.iloc[: p.trend_window - 1].isna().all()
    assert valid.iloc[-1] > 0.0  # a clean uptrend must produce a live long score


def test_chop_gate_zeroes_score():
    p = ScoreParams(er_min=0.3)
    # symmetric triangle wave: high path length, zero net move => ER ~ 0
    t = np.arange(800)
    closes = 3.0 + 0.05 * np.abs((t % 40) - 20)
    frame = ind.add_all(_frame(closes), p)
    s = score_mod.compute(frame, p)
    # after warmup, ER gate must hold the score at zero almost everywhere
    assert (s.dropna().iloc[p.trend_window:] == 0.0).mean() > 0.9


def test_trend_filter_blocks_downtrend():
    p = ScoreParams()
    closes = 3.0 * np.exp(np.cumsum(np.full(600, -0.002)))  # clean downtrend
    frame = ind.add_all(_frame(closes), p)
    s = score_mod.compute(frame, p)
    assert (s.dropna() <= 0.0).all()  # never a long signal below the trend MA


def test_score_is_causal(klines_1m):
    from near_bot import data

    p = ScoreParams(trend_window=50, mom_slow=40, vol_window=20, bo_window=20,
                    er_window=20, ofi_window=10, atr_period=10)
    bars = data.aggregate(klines_1m, 60, drop_incomplete=True)
    full = score_mod.compute(ind.add_all(bars, p), p)
    cut = len(bars) // 2
    part = score_mod.compute(ind.add_all(bars.iloc[: cut + 5], p), p)
    a = full.iloc[: cut - 1].to_numpy()
    b = part.iloc[: cut - 1].to_numpy()
    assert np.allclose(a, b, equal_nan=True)

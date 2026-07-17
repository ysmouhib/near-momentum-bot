"""Indicator correctness and, crucially, *causality*.

The causality tests are the load-bearing ones: if any indicator used future
data, the walk-forward validator would silently leak, and every OOS number
the project reports would be fiction.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from near_bot import indicators as ind


def test_sma_matches_manual():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ind.sma(s, 3)
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == 2.0
    assert out.iloc[4] == 4.0


def test_efficiency_ratio_bounds():
    straight = pd.Series(np.arange(1.0, 50.0))  # perfectly straight trend
    er = ind.efficiency_ratio(straight, 10)
    assert np.allclose(er.dropna(), 1.0)

    pingpong = pd.Series([1.0, 2.0] * 25)  # maximal chop
    er2 = ind.efficiency_ratio(pingpong, 10)
    assert er2.dropna().max() < 0.15


def test_donchian_position_bounds_and_extremes():
    n = 60
    close = pd.Series(np.linspace(1, 2, n))
    high = close * 1.01
    low = close * 0.99
    pos = ind.donchian_position(high, low, close, 20)
    assert pos.dropna().between(-1, 1).all()
    # a monotone rise closes near the top of its window range
    assert pos.iloc[-1] > 0.85


def test_tsmom_sign_and_scale():
    up = pd.Series(3.0 * np.exp(np.cumsum(np.full(300, 0.001))))
    z = ind.tsmom(up, 36, 48)
    assert z.iloc[-1] > 0  # persistent rise => positive momentum
    down = pd.Series(3.0 * np.exp(np.cumsum(np.full(300, -0.001))))
    z2 = ind.tsmom(down, 36, 48)
    assert z2.iloc[-1] < 0


def test_ofi_zscore_recovers_sign():
    rng = np.random.default_rng(5)
    vol = pd.Series(rng.lognormal(9, 0.2, 200))
    share = np.clip(0.7 + rng.normal(0, 0.03, 200), 0.05, 0.95)  # persistent buying
    z = ind.ofi_zscore(pd.Series(vol * share), vol, 24)
    assert z.iloc[-1] > 1.0


def test_causality_no_future_leak(klines_1m):
    """Mutating the future must not change any past indicator value."""
    from near_bot import data
    from near_bot.config import ScoreParams

    p = ScoreParams(trend_window=50, mom_slow=40, vol_window=20, bo_window=20,
                    er_window=20, ofi_window=10, atr_period=10)
    bars = data.aggregate(klines_1m, 60, drop_incomplete=True)
    full = ind.add_all(bars, p)

    cut = len(bars) // 2
    mutant = bars.copy()
    mutant.loc[cut:, ["open", "high", "low", "close"]] *= 5.0
    mutant.loc[cut:, "taker_buy_volume"] = 0.0
    part = ind.add_all(mutant.iloc[: cut + 5], p)

    cols = ["mom_fast", "mom_slow", "breakout", "ofi_z", "er", "trend_ma", "atr", "vol_bar"]
    a = full[cols].iloc[: cut - 1].to_numpy()
    b = part[cols].iloc[: cut - 1].to_numpy()
    assert np.allclose(a, b, equal_nan=True)


def test_bars_per_year():
    assert ind.bars_per_year(60) == 8760.0
    assert ind.bars_per_year(1) == 525600.0

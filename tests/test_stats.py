"""Stats module: bootstrap determinism, deflated Sharpe, decision counting."""

from __future__ import annotations

import numpy as np
import pytest

from near_bot import stats
from near_bot.backtest import Trade


def test_bootstrap_ci_deterministic_and_covers_truth():
    rng = np.random.default_rng(1)
    x = rng.normal(0.001, 0.01, 2000)  # per-bar mean 0.001
    pt, lo, hi = stats.bootstrap_ci(x, stats.mean_stat, n_boot=500, block=20, seed=3)
    assert lo < pt < hi
    assert lo < 0.001 < hi
    # same seed -> identical interval
    again = stats.bootstrap_ci(x, stats.mean_stat, n_boot=500, block=20, seed=3)
    assert again == (pt, lo, hi)


def test_block_resample_preserves_length_and_values():
    rng = np.random.default_rng(2)
    x = np.arange(100, dtype=float)
    out = stats._block_resample(x, block=7, rng=rng)
    assert len(out) == len(x)
    assert set(out) <= set(x)


def test_sharpe_stat_scales_with_bar_minutes():
    x = np.array([0.001, -0.0005, 0.0012, 0.0008, -0.0002] * 100)
    s60 = stats.sharpe_stat(60)(x)
    s15 = stats.sharpe_stat(15)(x)
    assert s60 > 0
    assert s15 == pytest.approx(s60 * 2.0)  # sqrt(4x bars per year)


def test_expected_max_sharpe_grows_with_trials():
    assert stats.expected_max_sharpe(1, 0.01) == 0.0
    e10 = stats.expected_max_sharpe(10, 0.01)
    e100 = stats.expected_max_sharpe(100, 0.01)
    assert 0.0 < e10 < e100


def test_deflated_sharpe_penalises_selection():
    rng = np.random.default_rng(5)
    x = rng.normal(0.002, 0.01, 500)  # mildly positive per-bar edge
    sd = x.std()
    sr = float(x.mean() / sd)
    sk, ku = stats.skewness(x), stats.kurtosis(x)
    few = stats.deflated_sharpe(sr, len(x), [0.0, 0.1, -0.1], skew=sk, kurt=ku)
    many = stats.deflated_sharpe(sr, len(x), list(rng.normal(0, 0.3, 94)),
                                 skew=sk, kurt=ku)
    assert 0.0 <= few["dsr"] <= 1.0
    assert 0.0 <= many["dsr"] <= 1.0
    assert many["dsr"] < few["dsr"]          # more trials -> harsher benchmark
    assert many["sr_star"] > few["sr_star"]


def test_skew_kurt_normal():
    rng = np.random.default_rng(11)
    x = rng.normal(0.0, 1.0, 5000)
    assert abs(stats.skewness(x)) < 0.1
    assert abs(stats.kurtosis(x) - 3.0) < 0.3
    assert stats.skewness(np.array([1.0, 1.0])) == 0.0
    assert stats.kurtosis(np.array([1.0, 2.0])) == 3.0


def _trade(entry, exit_, weighted_net):
    return Trade(entry_time=entry, exit_time=exit_, entry_price=1.0,
                 exit_price=1.0, weight=1.0, reason_out="x",
                 gross_pct=weighted_net, net_pct=weighted_net,
                 weighted_net=weighted_net, bars_held=exit_ - entry)


def test_decision_clusters_merge_overlaps():
    trades = [
        _trade(10, 20, 0.01),   # overlaps with next two: one decision
        _trade(12, 18, 0.005),
        _trade(19, 25, -0.004),
        _trade(40, 50, 0.02),   # disjoint: second decision
    ]
    dc = stats.decision_clusters(trades)
    assert dc.n_eff == 2
    pnls = dc.cluster_pnls()
    assert pnls[0] == pytest.approx(0.011)
    assert pnls[1] == pytest.approx(0.02)


def test_decision_clusters_empty():
    assert stats.decision_clusters([]).n_eff == 0

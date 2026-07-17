"""Walk-forward validator tests: window discipline, ensemble, determinism."""

from __future__ import annotations

import numpy as np

from near_bot import data
from near_bot import walkforward as wf
from near_bot.config import Config


def _cfg():
    cfg = Config()
    cfg.aggregate_minutes = 15  # 800 bars from the fixture: enough for windows
    return cfg


def _bars(klines_1m, cfg):
    return data.aggregate(klines_1m, cfg.aggregate_minutes, drop_incomplete=True)


def test_windows_are_train_then_test_with_embargo(klines_1m):
    cfg = _cfg()
    bars = _bars(klines_1m, cfg)
    res = wf.run(bars, cfg, train_bars=400, test_bars=150, embargo_bars=20)
    assert res.windows
    for w in res.windows:
        tr, te = w.train_range, w.test_range
        assert tr[1] + 20 == te[0]  # embargo gap respected
        assert te[1] - te[0] == 150


def test_oos_covers_only_test_windows(klines_1m):
    cfg = _cfg()
    bars = _bars(klines_1m, cfg)
    train, test = 400, 150
    res = wf.run(bars, cfg, train_bars=train, test_bars=test)
    n_windows = len([w for w in res.windows if w.combos is not None])
    if n_windows:
        assert len(res.oos_returns) == n_windows * test


def test_top_k_ensemble_averages(klines_1m):
    cfg = _cfg()
    bars = _bars(klines_1m, cfg)
    solo = wf.run(bars, cfg, train_bars=400, test_bars=150, top_k=1)
    ens = wf.run(bars, cfg, train_bars=400, test_bars=150, top_k=3)
    for w in ens.windows:
        if w.combos is not None:
            assert 1 <= len(w.combos) <= 3
    # more legs traded -> at least as many OOS trade records
    assert sum(w.out_metrics.get("n_trades", 0) for w in ens.windows) >= \
        sum(w.out_metrics.get("n_trades", 0) for w in solo.windows)


def test_min_trades_gate_skips_dead_windows(klines_1m):
    cfg = _cfg()
    bars = _bars(klines_1m, cfg)
    res = wf.run(bars, cfg, train_bars=400, test_bars=150, min_trades=10**6)
    assert all(w.combos is None for w in res.windows)
    assert res.oos_metrics.get("n_trades", 0) == 0


def test_determinism(klines_1m):
    cfg = _cfg()
    bars = _bars(klines_1m, cfg)
    a = wf.run(bars, cfg, train_bars=400, test_bars=150)
    b = wf.run(bars, cfg, train_bars=400, test_bars=150)
    assert np.allclose(a.oos_returns.to_numpy(), b.oos_returns.to_numpy())


def test_grid_validation_rejects_unknown_params(klines_1m):
    cfg = _cfg()
    bars = _bars(klines_1m, cfg)
    try:
        wf.run(bars, cfg, grid={"not_a_param": [1, 2]}, train_bars=400, test_bars=150)
    except ValueError as exc:
        assert "not_a_param" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for unknown grid parameter")

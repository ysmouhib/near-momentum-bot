"""Backtester honesty tests.

These encode the invariants a trustworthy backtester must have. The most
important: raising costs must never improve results, and changing the future
must never change the past (no look-ahead).
"""

from __future__ import annotations


import numpy as np

from near_bot import backtest as bt
from near_bot import data, indicators
from near_bot.config import Config


def _cfg(**over):
    cfg = Config()
    cfg.aggregate_minutes = 15  # 800 bars from the fixture: enough for warmup
    for k, v in over.items():
        setattr(cfg.strategy, k, v)
    return cfg


def _frame(klines_1m, cfg):
    bars = data.aggregate(klines_1m, cfg.aggregate_minutes, drop_incomplete=True)
    return indicators.add_all(bars, cfg.strategy)


def test_trades_exist_and_metrics_complete(klines_1m):
    cfg = _cfg()
    res = bt.run(_frame(klines_1m, cfg), cfg)
    m = res.metrics
    assert m["n_trades"] > 0
    for key in ("total_return", "sharpe", "max_drawdown", "exposure",
                "buy_hold", "profit_factor", "cost_drag", "turnover"):
        assert key in m
    assert -1.0 <= m["max_drawdown"] <= 0.0
    assert 0.0 <= m["exposure"] <= 1.0


def test_higher_costs_never_help(klines_1m):
    cfg = _cfg()
    frame = _frame(klines_1m, cfg)
    cheap = bt.run(frame, cfg).metrics["total_return"]
    dear_cfg = _cfg()
    dear_cfg.costs.taker_fee *= 2
    dear_cfg.costs.slippage *= 2
    dear = bt.run(frame, dear_cfg).metrics["total_return"]
    assert dear <= cheap + 1e-12


def test_no_lookahead(klines_1m):
    """Truncating the data must not change any trade that closed before the cut."""
    cfg = _cfg()
    frame = _frame(klines_1m, cfg)
    full = bt.run(frame, cfg)
    cut = int(len(frame) * 0.7)
    part = bt.run(frame.iloc[:cut], cfg)
    # the truncated run may force-close a final position (end_of_data); ignore it
    part_closed = [t for t in part.trades if t.reason_out != "end_of_data"]
    full_closed = [t for t in full.trades if t.exit_time <= frame.iloc[cut - 1]["timestamp"]]
    assert len(part_closed) == len(full_closed)
    for a, b in zip(part_closed, full_closed, strict=True):
        assert a.entry_time == b.entry_time and a.exit_time == b.exit_time
        assert a.entry_price == b.entry_price and a.exit_price == b.exit_price


def test_hysteresis_reduces_churn(klines_1m):
    frame_cfg = _cfg()
    frame = _frame(klines_1m, frame_cfg)
    tight = _cfg(theta_in=0.25, theta_out=0.0)
    loose = _cfg(theta_in=0.25, theta_out=0.24)  # almost no band -> hair-trigger exits
    n_tight = bt.run(frame, tight).metrics["n_trades"]
    n_loose = bt.run(frame, loose).metrics["n_trades"]
    assert n_tight <= n_loose


def test_vol_targeting_sizes_down_in_vol():
    from near_bot.backtest import position_weight

    calm = position_weight(0.002, 60, target_vol=0.40, min_weight=0.05)
    wild = position_weight(0.010, 60, target_vol=0.40, min_weight=0.05)
    assert 0 < wild < calm <= 1.0
    assert position_weight(np.nan, 60, 0.40, 0.05) == 0.0
    # extreme calm caps at 100% notional
    assert position_weight(1e-6, 60, 0.40, 0.05) == 1.0


def test_open_position_marked_to_market(klines_1m):
    cfg = _cfg(max_hold_bars=10**9, trail_atr=10**9, theta_out=-2.0)  # never exit
    res = bt.run(_frame(klines_1m, cfg), cfg)
    if res.trades:  # any entry must be closed artificially at the last bar
        assert res.trades[-1].reason_out == "end_of_data"


def test_equity_consistent_with_bar_returns(klines_1m):
    cfg = _cfg()
    res = bt.run(_frame(klines_1m, cfg), cfg)
    rebuilt = float(np.prod(1.0 + res.bar_returns.to_numpy()))
    assert abs(rebuilt - float(res.equity.iloc[-1])) < 1e-12


def _manual_frame(opens, closes, vol=0.001, atr=1.0):
    import pandas as pd

    n = len(opens)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h"),
        "open": opens, "high": [max(o, c) for o, c in zip(opens, closes, strict=True)],
        "low": [min(o, c) for o, c in zip(opens, closes, strict=True)],
        "close": closes, "vol_bar": vol, "atr": atr,
    })


def test_per_bar_accounting_hand_computed():
    """A single forced trade, checked against hand-computed per-bar returns."""
    import pandas as pd

    cfg = _cfg(theta_in=0.25, theta_out=0.0, trail_atr=1e9, max_hold_bars=10**9,
               target_vol=1e9, min_weight=0.0)  # weight pins to 1.0
    cfg.costs.taker_fee = 0.0
    cfg.costs.slippage = 0.0
    opens = [99.0, 99.5, 100.0, 104.0, 110.0]
    closes = [99.5, 99.8, 102.0, 105.0, 109.0]
    # score: enter at close of bar 1, hold bar 2, exit at close of bar 3
    score = pd.Series([np.nan, 0.5, 0.3, -0.1, np.nan])
    res = bt.run(_manual_frame(opens, closes), cfg, score=score)

    r = res.bar_returns.to_numpy()
    assert r[0] == 0.0 and r[1] == 0.0
    assert r[2] == 102.0 / 100.0 - 1.0          # entry bar: open -> close
    assert r[3] == 105.0 / 102.0 - 1.0          # holding bar: close -> close
    assert r[4] == 110.0 / 105.0 - 1.0          # exit bar: prev close -> open
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_price == 100.0 and t.exit_price == 110.0
    assert abs(t.gross_pct - 0.10) < 1e-12
    assert abs(t.net_pct - 0.10) < 1e-12
    assert t.weight == 1.0
    assert t.reason_out == "score_fade"


def test_fee_placement_hand_computed():
    """Fees land on the entry and exit bars only, exactly w*fee each side."""
    import pandas as pd

    cfg = _cfg(theta_in=0.25, theta_out=0.0, trail_atr=1e9, max_hold_bars=10**9,
               target_vol=1e9, min_weight=0.0)
    cfg.costs.taker_fee = 0.001
    cfg.costs.slippage = 0.0
    opens = [99.0, 99.5, 100.0, 104.0, 110.0]
    closes = [99.5, 99.8, 102.0, 105.0, 109.0]
    score = pd.Series([np.nan, 0.5, 0.3, -0.1, np.nan])
    res = bt.run(_manual_frame(opens, closes), cfg, score=score)
    r = res.bar_returns.to_numpy()
    assert abs(r[2] - (102.0 / 100.0 - 1.0 - 0.001)) < 1e-15
    assert abs(r[3] - (105.0 / 102.0 - 1.0)) < 1e-15
    assert abs(r[4] - (110.0 / 105.0 - 1.0 - 0.001)) < 1e-15
    t = res.trades[0]
    assert abs(t.net_pct - (0.10 - 0.002)) < 1e-15


def test_zero_cost_edge_case(klines_1m):
    cfg = _cfg()
    cfg.costs.taker_fee = 0.0
    cfg.costs.slippage = 0.0
    res = bt.run(_frame(klines_1m, cfg), cfg)
    m = res.metrics
    assert m["cost_drag"] == 0.0

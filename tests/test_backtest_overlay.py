"""Overlay-in-the-backtester tests: same accounting, new gates.

The load-bearing invariant: with no overlay and no armed series the loop is
byte-identical to the pure engine (that is what keeps the JS parity fixtures
meaningful). The rest pins overlay behaviour to hand-computed arithmetic on
hand-built frames, exactly like the v2 accounting tests.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from near_bot import backtest as bt
from near_bot import data, indicators
from near_bot.config import Config


def _cfg(**over):
    cfg = Config()
    cfg.aggregate_minutes = 15
    for k, v in over.items():
        setattr(cfg.strategy, k, v)
    return cfg


def _frame(klines_1m, cfg):
    bars = data.aggregate(klines_1m, cfg.aggregate_minutes, drop_incomplete=True)
    return indicators.add_all(bars, cfg.strategy)


def _manual_frame(opens, closes, vol=0.001, atr=1.0, start="2026-01-01"):
    n = len(opens)
    return pd.DataFrame({
        "timestamp": pd.date_range(start, periods=n, freq="1h"),
        "open": opens, "high": [max(o, c) for o, c in zip(opens, closes, strict=True)],
        "low": [min(o, c) for o, c in zip(opens, closes, strict=True)],
        "close": closes, "vol_bar": vol, "atr": atr,
    })


def test_overlay_off_is_byte_identical(klines_1m):
    cfg = _cfg()
    frame = _frame(klines_1m, cfg)
    a = bt.run(frame, cfg)
    b = bt.run(frame, cfg, overlay=None, armed=None)
    assert np.array_equal(a.bar_returns.to_numpy(), b.bar_returns.to_numpy())
    assert a.metrics == b.metrics  # no overlay keys leak into the pure path
    ka = [(t.entry_time, t.exit_time, t.entry_price, t.exit_price, t.weight)
          for t in a.trades]
    kb = [(t.entry_time, t.exit_time, t.entry_price, t.exit_price, t.weight)
          for t in b.trades]
    assert ka == kb


def test_overlay_sizing_is_risk_over_stop_distance():
    """Same frame as the v2 accounting test, but the overlay sizes w = 0.2:
    ATR = 1% of price, stop = 3 ATR -> w_risk = 0.006/0.03 = 0.20."""
    cfg = _cfg(theta_in=0.25, theta_out=0.0, max_hold_bars=10**9,
               target_vol=1e9, min_weight=0.0)
    cfg.costs.taker_fee = 0.0
    cfg.costs.slippage = 0.0
    opens = [99.0, 99.5, 100.0, 104.0, 110.0]
    closes = [99.5, 99.8, 102.0, 105.0, 109.0]
    score = pd.Series([np.nan, 0.5, 0.3, -0.1, np.nan])
    res = bt.run(_manual_frame(opens, closes), cfg, score=score,
                 overlay=cfg.overlay)
    r = res.bar_returns.to_numpy()
    w = 0.2
    assert r[2] == w * (102.0 / 100.0 - 1.0)
    assert r[3] == w * (105.0 / 102.0 - 1.0)
    assert r[4] == w * (110.0 / 105.0 - 1.0)
    t = res.trades[0]
    assert t.weight == w and t.reason_out == "score_fade"
    assert t.m_tier == 1.0 and t.m_cooldown == 1.0
    assert res.metrics["n_blocked_entries"] == 0
    assert res.metrics["n_full_stops"] == 0


def test_armed_false_blocks_entries(klines_1m):
    cfg = _cfg()
    frame = _frame(klines_1m, cfg)
    armed = pd.Series(True, index=frame.index)
    armed.iloc[-200:] = False  # disarmed for the tail
    gated = bt.run(frame, cfg, armed=armed)
    plain = bt.run(frame, cfg)
    # no trade may start inside the disarmed span
    cut = frame["timestamp"].iloc[-200]
    assert all(t.entry_time < cut for t in gated.trades)
    # everything before the disarmed span is untouched (causality of the gate)
    early_plain = [(t.entry_time, t.exit_time) for t in plain.trades
                   if t.exit_time < cut]
    early_gated = [(t.entry_time, t.exit_time) for t in gated.trades
                   if t.exit_time < cut]
    assert early_plain == early_gated


def _halt_frame():
    """27 hourly bars across two UTC days: enter, trail-stop out of a -12%
    position move (-2.4% equity at w=0.2 -> daily halt), then 20 blocked
    signals on day one and one permitted entry on day two."""
    opens = ([99.0, 99.5, 100.0, 90.0] + [95.0] * 23)
    closes = ([99.5, 99.8, 100.0, 88.0] + [95.0] * 23)
    return opens, closes


def test_daily_halt_blocks_rest_of_day_only():
    cfg = _cfg(theta_in=0.25, theta_out=0.0, max_hold_bars=10**9,
               target_vol=1e9, min_weight=0.0)
    cfg.costs.taker_fee = 0.0
    cfg.costs.slippage = 0.0
    opens, closes = _halt_frame()
    frame = _manual_frame(opens, closes)  # hourly from 2026-01-01 00:00
    score = pd.Series([np.nan, 0.5] + [0.3] * 25)
    res = bt.run(frame, cfg, score=score, overlay=cfg.overlay)

    assert res.metrics["n_day_halts"] == 1
    # bars 4..23 fire entry signals while halted (same UTC day): 20 blocks
    assert res.metrics["n_blocked_entries"] == 20
    assert len(res.trades) == 2
    t1, t2 = res.trades
    assert t1.reason_out == "trail_stop"
    assert t1.weight == 0.2  # 0.006 / (3*1/100)
    # second entry happens on the first bar of day two, fill at the next open
    assert str(t2.entry_time) == "2026-01-02 01:00:00"
    assert t2.reason_out == "end_of_data"
    assert t2.weight == 0.006 / (3.0 / 95.0)  # stop distance priced off 95


def test_full_stop_flattens_and_blocks_thereafter():
    cfg = _cfg(theta_in=0.25, theta_out=0.0, max_hold_bars=10**9,
               target_vol=1e9, min_weight=0.0)
    cfg.costs.taker_fee = 0.0
    cfg.costs.slippage = 0.0
    opens = [99.0, 99.5, 100.0, 69.0, 70.0, 70.0, 70.0, 70.0]
    closes = [99.5, 99.8, 100.0, 70.0, 70.0, 70.0, 70.0, 70.0]
    frame = _manual_frame(opens, closes, atr=0.1)  # tiny ATR -> w capped 0.40
    score = pd.Series([np.nan, 0.5, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3])
    res = bt.run(frame, cfg, score=score, overlay=cfg.overlay)

    assert res.metrics["n_full_stops"] == 1
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.reason_out == "full_stop"
    assert t.weight == 0.40           # exposure cap binds, not the risk budget
    assert t.entry_price == 100.0 and t.exit_price == 70.0
    # -30% on w=0.4 -> -12% equity: full stop; the three later signals blocked
    assert res.metrics["n_blocked_entries"] == 3
    assert abs(res.equity.iloc[-1] - 0.88) < 1e-12

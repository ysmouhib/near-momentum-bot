"""Executor overlay integration: entries gated by permission, sizing by the
risk budget, kills flatten, and restart reconciliation against balances.

All offline: a FakeBroker stands in for python-binance, and _fetch_frame is
monkeypatched to a hand-built frame so the score is exactly controlled.
"""

from __future__ import annotations

import pandas as pd
import pytest

from near_bot import journal
from near_bot import overlay as ov
from near_bot.config import Config
from near_bot.executor import Executor


class FakeBroker:
    def __init__(self, price=100.0, free_usdt=1000.0, free_base=0.0):
        self._price = price
        self.usdt = free_usdt
        self.base = free_base
        self.orders: list[tuple[str, float]] = []
        self.min_qty = None
        self.step_size = None
        self.min_notional = None
        self.fail_equity = False

    def price(self):
        return self._price

    def balance(self, asset):
        return (self.usdt, 0.0) if asset == "USDT" else (self.base, 0.0)

    def equity_usdt(self):
        if self.fail_equity:
            raise RuntimeError("simulated API failure")
        return self.usdt + self.base * self._price

    def round_qty(self, qty):
        return qty

    def buy(self, qty, price_hint=None):
        self.orders.append(("buy", qty))
        self.usdt -= qty * self._price
        self.base += qty
        return {"orderId": 1, "status": "FILLED",
                "fills": [{"price": str(self._price), "qty": str(qty)}]}

    def sell(self, qty, price_hint=None):
        self.orders.append(("sell", qty))
        self.usdt += qty * self._price
        self.base -= qty
        return {"orderId": 2, "status": "FILLED",
                "fills": [{"price": str(self._price), "qty": str(qty)}]}


def _frame(score, ts="2026-03-01 01:00", close=100.0, atr=1.0):
    n = len(score)
    return pd.DataFrame({
        "timestamp": pd.date_range(ts, periods=n, freq="-1h")[::-1],
        "open": close, "high": close, "low": close, "close": close,
        "vol_bar": 0.001, "atr": atr, "score": score,
        "mom_fast": 0.5, "mom_slow": 0.5, "breakout": 0.2, "ofi_z": 0.1,
        "er": 0.3, "trend_ma": 90.0,
    })


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.overlay.journal_path = str(tmp_path / "journal.csv")
    c.overlay.state_path = str(tmp_path / "state.json")
    return c


def _executor(cfg, broker, score=0.5, arm=False):
    if arm:
        state = ov.load_state(cfg.overlay.state_path)
        ov.arm(state, cfg.overlay)
        ov.save_state(state, cfg.overlay.state_path)
    ex = Executor(cfg, broker)
    ex._fetch_frame = lambda: _frame([score, score])
    return ex


def test_disarmed_executor_never_enters(cfg):
    ex = _executor(cfg, FakeBroker())  # fresh state = DISARMED
    ex.step()
    assert ex.position is None
    assert ex.broker.orders == []


def test_armed_entry_uses_overlay_sizing_and_cooldown(cfg):
    broker = FakeBroker(price=100.0, free_usdt=1000.0)
    ex = _executor(cfg, broker, arm=True)  # arm -> cooldown: x0.5 for 3 trades
    ex.step()
    assert broker.orders == [("buy", 1.0)]
    pos = ex.position
    # w = min(vol 1.0, risk 0.006/0.03 = 0.20, cap 0.40) x cooldown 0.5 = 0.10
    assert pos.weight == pytest.approx(0.10)
    assert pos.m_cooldown == 0.5
    # the position was persisted for restart recovery
    saved = ov.load_state(cfg.overlay.state_path).open_position
    assert saved["qty"] == 1.0 and saved["entry_price"] == 100.0


def test_engine_exit_journals_trade_and_counts_cooldown(cfg):
    broker = FakeBroker(price=100.0, free_usdt=1000.0)
    ex = _executor(cfg, broker, arm=True)
    ex.step()
    assert ex.position is not None
    ex._fetch_frame = lambda: _frame([-0.1, -0.1])  # score fades below theta_out
    ex.step()
    assert ex.position is None
    assert broker.orders[-1][0] == "sell"
    state = ov.load_state(cfg.overlay.state_path)
    assert state.cooldown_left == cfg.overlay.cooldown_trades_after_arm - 1
    assert state.open_position is None
    events = journal.load(cfg.overlay.journal_path)
    trades = events[events["event"] == "trade"]
    assert len(trades) == 1
    assert "weighted_pct=" in trades.iloc[0]["context"]


def test_reconcile_recovers_position_from_state(cfg):
    state = ov.OverlayState(state=ov.ARMED, hwm=1000.0)
    state.open_position = {"qty": 2.0, "entry_price": 95.0, "weight": 0.2,
                           "peak_close": 100.0, "entry_bar_time": "2026-02-28",
                           "bars_held": 5, "m_tier": 1.0, "m_cooldown": 0.5}
    ov.save_state(state, cfg.overlay.state_path)
    broker = FakeBroker(price=100.0, free_usdt=810.0, free_base=2.0)
    ex = Executor(cfg, broker)
    assert ex.position is not None
    assert ex.position.entry_price == 95.0 and ex.position.qty == 2.0


def test_reconcile_clears_phantom_position(cfg):
    state = ov.OverlayState(state=ov.ARMED, hwm=1000.0)
    state.open_position = {"qty": 2.0, "entry_price": 95.0, "weight": 0.2,
                           "peak_close": 100.0, "entry_bar_time": "2026-02-28"}
    ov.save_state(state, cfg.overlay.state_path)
    broker = FakeBroker(free_base=0.0)  # exchange says flat
    ex = Executor(cfg, broker)
    assert ex.position is None
    assert ov.load_state(cfg.overlay.state_path).open_position is None


def test_reconcile_blocks_entries_on_unexplained_base_balance(cfg):
    broker = FakeBroker(free_base=3.0)  # bot has no record of this
    ex = _executor(cfg, broker, arm=True)
    assert ex._entries_blocked
    ex.step()
    assert broker.orders == []  # nothing bought despite a live entry signal


def test_full_stop_flattens_and_waits_for_human(cfg):
    broker = FakeBroker(price=100.0, free_usdt=600.0)
    ex = _executor(cfg, broker, arm=True)
    ex.step()  # buys 1.0 @ 100, equity still 1000, HWM anchored high
    assert ex.position is not None
    # price collapses 30%: equity = 0 + 1*70 = 700 vs HWM >= 1000 -> -30%
    broker._price = 70.0
    state = ov.load_state(cfg.overlay.state_path)
    state.hwm = 1000.0
    ov.save_state(state, cfg.overlay.state_path)
    ex.ov_state = ov.load_state(cfg.overlay.state_path)
    ex.step()
    assert ex.position is None
    assert broker.orders[-1] == ("sell", 0.6)  # the position bought with equity 600
    state = ov.load_state(cfg.overlay.state_path)
    assert state.state == ov.STOPPED
    events = journal.load(cfg.overlay.journal_path)
    assert "auto_full_stop" in set(events["event"])
    assert "auto_flatten" in set(events["event"])
    # STOPPED blocks entries even with the score still hot
    ex.step()
    assert len([o for o in broker.orders if o[0] == "buy"]) == 1


def test_restart_keeps_trailing_stop_and_bar_count(cfg):
    """Peak close and bars held move every bar; a restart must resume them,
    not reload the values written at the fill."""
    broker = FakeBroker(price=100.0, free_usdt=1000.0)
    ex = _executor(cfg, broker, arm=True)
    ex.step()  # enter @ 100 on the 01:00 bar
    for ts, close in (("2026-03-01 02:00", 110.0), ("2026-03-01 03:00", 120.0)):
        ex._fetch_frame = lambda ts=ts, close=close: _frame([0.5, 0.5], ts=ts, close=close)
        ex.step()
    assert ex.position.peak_close == 120.0 and ex.position.bars_held == 2

    restarted = Executor(cfg, broker)  # same exchange balances, fresh process
    pos = restarted.position
    assert pos.peak_close == 120.0 and pos.bars_held == 2
    assert restarted.last_bar_time == pd.Timestamp("2026-03-01 03:00")
    # the bar that was current at shutdown is not counted twice
    restarted._fetch_frame = lambda: _frame([0.5, 0.5], ts="2026-03-01 03:00", close=120.0)
    restarted.step()
    assert restarted.position.bars_held == 2
    # trail = 120 - 3 * ATR(1.0) = 117: a 116 close exits (a trail reloaded
    # from the fill would sit at 97 and miss it). Score 0.2 sits inside the
    # no-trade band: no score-fade exit, no same-cycle re-entry.
    restarted._fetch_frame = lambda: _frame([0.2, 0.2], ts="2026-03-01 04:00", close=116.0)
    restarted.step()
    assert restarted.position is None
    assert broker.orders[-1][0] == "sell"
    trades = journal.load(cfg.overlay.journal_path)
    assert "exit trail_stop" in set(trades["note"])


def test_overlay_failure_fails_closed(cfg):
    broker = FakeBroker()
    broker.fail_equity = True
    ex = _executor(cfg, broker, arm=True)
    ex.step()  # overlay evaluation raises -> cycle behaves DISARMED
    assert ex.position is None
    assert broker.orders == []


def test_engine_only_mode_unchanged(cfg):
    cfg.overlay.enabled = False
    broker = FakeBroker(price=100.0, free_usdt=1000.0)
    ex = Executor(cfg, broker)
    ex._fetch_frame = lambda: _frame([0.5, 0.5])
    ex.step()
    # v2 sizing: w = 1.0 (vol target, capped) -> notional = 0.95 * 1000
    assert broker.orders == [("buy", 9.5)]
    assert ex.position.weight == 1.0

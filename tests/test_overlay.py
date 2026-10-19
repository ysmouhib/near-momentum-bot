"""Overlay state machine: every transition of the kill-switch ladder, pinned.

These tests exist because the overlay is the part of the system that acts
while the human is asleep. The ladder (docs/DESIGN.md §4.2): −2% on the UTC
day halts entries, −5% from HWM halves size (clears at −3%, hysteresis),
−10% flattens and waits for a human re-arm, and every arm starts a
half-size transition cooldown.
"""

from __future__ import annotations

import pytest

from near_bot import overlay as ov
from near_bot.config import Config


def _p():
    return Config().overlay


def _armed_state(p):
    s = ov.OverlayState()
    ov.arm(s, p)
    return s


def test_starts_disarmed_and_blocks_entries():
    s = ov.OverlayState()
    assert s.state == ov.DISARMED
    assert not ov.can_enter(s, "2026-01-01")


def test_arm_starts_transition_cooldown():
    p = _p()
    s = _armed_state(p)
    assert s.state == ov.ARMED
    assert s.cooldown_left == p.cooldown_trades_after_arm
    assert ov.multiplier(s, p) == p.transition_size_mult


def test_rearm_while_armed_is_noop_event():
    p = _p()
    s = _armed_state(p)
    events = ov.arm(s, p)
    assert events == [("arm", "already armed (no-op)")]


def test_disarm_blocks_entries_but_not_exits():
    p = _p()
    s = _armed_state(p)
    ov.disarm(s)
    assert s.state == ov.DISARMED
    assert not ov.can_enter(s, "2026-01-01")
    # no flatten: disarming must never touch an open position
    assert not ov.should_flatten(s)


def test_daily_loss_halt_blocks_until_next_utc_day():
    p = _p()
    s = _armed_state(p)
    ov.evaluate(s, p, equity=1.0, date_str="2026-03-01")
    events = ov.evaluate(s, p, equity=0.975, date_str="2026-03-01")
    assert any(e == "auto_halt_day" for e, _ in events)
    assert not ov.can_enter(s, "2026-03-01")
    # halt fires once per day, not every bar
    assert ov.evaluate(s, p, equity=0.974, date_str="2026-03-01") == []
    # the next UTC day lifts it
    ov.evaluate(s, p, equity=0.974, date_str="2026-03-02")
    assert ov.can_enter(s, "2026-03-02")


def test_half_size_tier_with_recovery_hysteresis():
    p = _p()
    s = _armed_state(p)
    s.cooldown_left = 0
    ov.evaluate(s, p, equity=1.0, date_str="2026-03-01")
    events = ov.evaluate(s, p, equity=0.946, date_str="2026-03-01")  # -5.4% from HWM
    assert any(e == "auto_half_on" for e, _ in events)
    assert ov.multiplier(s, p) == ov.TIER_MULT
    # -4%: still inside the hysteresis band -> tier stays on
    assert ov.evaluate(s, p, equity=0.96, date_str="2026-03-02") == []
    assert s.half_tier
    # above the -3% clear line -> tier off
    events = ov.evaluate(s, p, equity=0.975, date_str="2026-03-02")
    assert any(e == "auto_half_off" for e, _ in events)
    assert not s.half_tier
    assert ov.multiplier(s, p) == 1.0


def test_tier_does_not_flap_at_boundary():
    p = _p()
    s = _armed_state(p)
    s.cooldown_left = 0
    ov.evaluate(s, p, equity=1.0, date_str="2026-03-01")
    for eq in (0.9499, 0.9501, 0.9499, 0.9501):
        ov.evaluate(s, p, equity=eq, date_str="2026-03-01")
    assert s.half_tier  # crossed once, never cleared by sub--3% bounces


def test_full_stop_requires_human_rearm():
    p = _p()
    s = _armed_state(p)
    ov.evaluate(s, p, equity=1.0, date_str="2026-03-01")
    events = ov.evaluate(s, p, equity=0.89, date_str="2026-03-01")  # -11%
    assert any(e == "auto_full_stop" for e, _ in events)
    assert s.state == ov.STOPPED
    assert ov.should_flatten(s)
    assert not ov.can_enter(s, "2026-03-01")
    # further losses do not re-fire the event
    assert ov.evaluate(s, p, equity=0.85, date_str="2026-03-02") == []
    assert s.state == ov.STOPPED
    # only the human gets the bot out of STOPPED, into the cooldown
    ov.arm(s, p)
    assert s.state == ov.ARMED
    assert s.cooldown_left == p.cooldown_trades_after_arm


def test_multipliers_compose_with_floor():
    p = _p()
    s = _armed_state(p)
    ov.evaluate(s, p, equity=1.0, date_str="2026-03-01")
    ov.evaluate(s, p, equity=0.94, date_str="2026-03-01")  # tier on
    # cooldown (0.5) x tier (0.5) = 0.25 = the floor
    assert ov.multiplier(s, p) == p.min_multiplier


def test_cooldown_counts_closed_trades():
    p = _p()
    s = _armed_state(p)
    for _ in range(p.cooldown_trades_after_arm - 1):
        assert ov.on_trade_closed(s) == []
    assert ov.on_trade_closed(s)[0][0] == "cooldown_done"
    assert ov.multiplier(s, p) == 1.0


def test_entry_weight_risk_budget_and_caps():
    p = _p()
    # ATR = 1% of price, trail = 3 ATR -> stop distance 3%; R = 0.6% -> w = 0.20
    w = ov.entry_weight(w_vol=1.0, entry_price=100.0, atr=1.0, trail_atr=3.0,
                        open_exposure=0.0, mult=1.0, p=p, min_weight=0.05)
    assert w == pytest.approx(0.20)
    # the exposure cap binds a huge vol-target weight
    w = ov.entry_weight(w_vol=1.0, entry_price=100.0, atr=0.1, trail_atr=3.0,
                        open_exposure=0.0, mult=1.0, p=p, min_weight=0.05)
    assert w == pytest.approx(p.max_exposure_frac)
    # half multiplier flows through
    w = ov.entry_weight(w_vol=1.0, entry_price=100.0, atr=1.0, trail_atr=3.0,
                        open_exposure=0.0, mult=0.5, p=p, min_weight=0.05)
    assert w == pytest.approx(0.10)
    # below the dust floor -> skip
    w = ov.entry_weight(w_vol=0.04, entry_price=100.0, atr=1.0, trail_atr=3.0,
                        open_exposure=0.0, mult=1.0, p=p, min_weight=0.05)
    assert w == 0.0
    # degenerate inputs -> no trade
    assert ov.entry_weight(1.0, 0.0, 1.0, 3.0, 0.0, 1.0, p, 0.05) == 0.0
    assert ov.entry_weight(1.0, 100.0, 0.0, 3.0, 0.0, 1.0, p, 0.05) == 0.0


def test_state_persistence_roundtrip(tmp_path):
    p = _p()
    s = _armed_state(p)
    ov.evaluate(s, p, equity=1.05, date_str="2026-03-01")
    s.open_position = {"qty": 10.0, "entry_price": 3.21}
    path = tmp_path / "state" / "overlay_state.json"
    ov.save_state(s, path)
    loaded = ov.load_state(path)
    assert loaded.state == ov.ARMED
    assert loaded.hwm == pytest.approx(1.05)
    assert loaded.open_position == {"qty": 10.0, "entry_price": 3.21}
    # missing file -> fresh, disarmed
    fresh = ov.load_state(tmp_path / "nope.json")
    assert fresh.state == ov.DISARMED
    # no tmp file left behind after the atomic rename
    assert not (tmp_path / "state" / "overlay_state.json.tmp").exists()

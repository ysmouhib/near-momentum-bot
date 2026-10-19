"""Synthetic-regime sweep (experiments/regime_sweep.py, DESIGN.md §7.4).

The sweep exists to validate the overlay plumbing end to end, so the tests
pin it the same way: on hand-built windows with a known good/bad P&L
contrast, the Monte Carlo break-even must land on the hand-computed one; the
three replay cases must behave as the overlay says (continuing armed = no
cooldown, fresh arm = cooldown, disarmed = no trades); and the script must
end its report in the mandatory verdict and caveat.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from conftest import make_1m_klines

from near_bot import data
from near_bot import walkforward as wf
from near_bot.backtest import Trade
from near_bot.config import Config

_PATH = Path(__file__).resolve().parent.parent / "experiments" / "regime_sweep.py"
_spec = importlib.util.spec_from_file_location("regime_sweep", _PATH)
rs = importlib.util.module_from_spec(_spec)
sys.modules["regime_sweep"] = rs  # dataclasses resolve their module by name
_spec.loader.exec_module(rs)


def _trade(pnl):
    return Trade(entry_time=0, exit_time=1, entry_price=1.0, exit_price=1.0, weight=1.0,
                 reason_out="x", gross_pct=pnl, net_pct=pnl, weighted_net=pnl, bars_held=1)


def _rep(steady_sum, fresh_sum=None, trade_pnl=0.0, n_trades=2):
    fresh_sum = steady_sum if fresh_sum is None else fresh_sum

    def case(s):
        return np.array([s / 2, s / 2]), [_trade(trade_pnl)] * n_trades

    return rs.WindowReplay((0, 2), engine_return=steady_sum, n_legs=1,
                           variants={rs.STEADY: case(steady_sum), rs.FRESH: case(fresh_sum),
                                     rs.OFF: (np.zeros(2), [])})


def test_make_schedule_hits_the_requested_accuracy():
    good = np.random.default_rng(0).random(20000) < 0.5
    assert np.array_equal(rs.make_schedule(good, 1.0, np.random.default_rng(1)), good)
    assert np.array_equal(rs.make_schedule(good, 0.0, np.random.default_rng(1)), ~good)
    s = rs.make_schedule(good, 0.7, np.random.default_rng(2))
    assert abs((s == good).mean() - 0.7) < 0.02
    again = rs.make_schedule(good, 0.7, np.random.default_rng(2))
    assert np.array_equal(s, again)


def test_assemble_picks_fresh_steady_and_disarmed_cases():
    reps = [_rep(0.01, fresh_sum=0.02) for _ in range(4)]
    out = rs.assemble(reps, [True, True, False, True])  # starts DISARMED
    # fresh arm, continuing, disarmed, fresh arm again
    assert out["sum_return"] == pytest.approx(0.02 + 0.01 + 0.0 + 0.02)
    assert out["n_trades"] == 6 and out["armed_share"] == 0.75
    steady = rs.assemble(reps, [True] * 4, armed_before=True)
    assert steady["sum_return"] == pytest.approx(0.04)


def test_sweep_break_even_matches_hand_arithmetic():
    """10 good windows (+2%, trades +1%) and 10 bad (-3%, trades -1.5%):
    E[sum](p) = 0.2 p - 0.3 (1 - p) = 0 at p* = 0.6, and L/(W+L) = 0.6."""
    reps = []
    for _ in range(10):
        reps += [_rep(0.02, trade_pnl=0.01), _rep(-0.03, trade_pnl=-0.015)]
    swept = rs.sweep(reps, [0.5, 0.6, 0.7, 0.8], n_schedules=2000, seed=3)
    be = rs.break_evens(reps, swept)
    assert be["arithmetic"] == pytest.approx(0.6)
    assert be["design_formula"] == pytest.approx(0.6)
    assert be["W"] == pytest.approx(0.01) and be["L"] == pytest.approx(-0.015)
    assert abs(be["empirical"] - 0.6) < 0.02
    # oracle bounds bracket every accuracy
    lo = swept["oracle"][0.0]["sum_return"]["mean"]
    hi = swept["oracle"][1.0]["sum_return"]["mean"]
    assert lo == pytest.approx(-0.3) and hi == pytest.approx(0.2)
    for p in (0.5, 0.6, 0.7, 0.8):
        assert lo < swept[p]["sum_return"]["mean"] < hi
    text = "\n".join(rs.verdict_lines(reps, swept, be))
    assert "Plumbing: consistent" in text
    assert "statistically thin" in text  # 20 windows < 30
    assert "clairvoyant" in text


def _replays():
    cfg = Config()
    cfg.aggregate_minutes = 15
    bars = data.aggregate(make_1m_klines(24000, seed=7), 15, drop_incomplete=True)
    res = wf.run(bars, cfg, train_bars=384, test_bars=96, top_k=3)
    return res, rs.replay_windows(bars, cfg, res)


def test_replay_cases_follow_the_overlay_contract():
    res, reps = _replays()
    traded = [w for w in res.windows if w.combos is not None]
    assert len(reps) == len(traded) > 0
    any_fresh_trade = False
    for rep, w in zip(reps, traded, strict=True):
        assert rep.engine_return == w.out_metrics["total_return"]
        rets_off, trades_off = rep.variants[rs.OFF]
        assert not trades_off and not rets_off.any()  # disarmed: flat all window
        assert all(t.m_cooldown == 1.0 for t in rep.variants[rs.STEADY][1])
        fresh = rep.variants[rs.FRESH][1]
        if fresh:
            any_fresh_trade = True
            assert any(t.m_cooldown == 0.5 for t in fresh)  # first trades after an arm
    assert any_fresh_trade


def test_main_writes_report_with_verdict(tmp_path, capsys):
    import yaml

    csv = tmp_path / "NEARUSDT_1m_test.csv"
    data.save_csv(make_1m_klines(24000, seed=7), csv)
    cfg = Config()
    cfg.aggregate_minutes = 15
    cfg_file = tmp_path / "config.yaml"
    with cfg_file.open("w") as fh:
        yaml.safe_dump(cfg.to_dict(), fh)
    out = tmp_path / "reports" / "regime_sweep.md"
    rc = rs.main(["--csv", str(csv), "--config", str(cfg_file), "--train-days", "4",
                  "--test-days", "1", "--schedules", "30", "--out", str(out)])
    assert rc == 0
    report = out.read_text(encoding="utf-8")
    for needle in ("## Value of accuracy", "| 100% |", "| 0% |", "## Break-even accuracy",
                   "Theoretical (DESIGN.md §0", "## Verdict", "Caveat:", "Overall:"):
        assert needle in report
    assert report.rstrip().endswith("Not financial advice.")
    assert "## Verdict" in capsys.readouterr().out

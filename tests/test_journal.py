"""Journal and human-overlay CLI: roundtrip, scoring, reporting.

The journal is the instrument that measures the human's regime-call accuracy,
so these tests pin its append-only discipline, the arm/disarm state writes,
and the scoring pipeline (replayed engine P&L over forward windows).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from near_bot import cli, data, indicators, journal
from near_bot.config import Config

from conftest import make_1m_klines


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    c = Config()
    c.overlay.journal_path = str(tmp_path / "state" / "journal.csv")
    c.overlay.state_path = str(tmp_path / "state" / "overlay_state.json")
    c.validation.bootstrap_reps = 200
    return c


def _config_file(cfg: Config, tmp_path) -> str:
    import yaml

    path = tmp_path / "config.yaml"
    with path.open("w") as fh:
        yaml.safe_dump(cfg.to_dict(), fh)
    return str(path)


def test_append_load_roundtrip(cfg):
    journal.append(cfg.overlay.journal_path, "arm", "armed", "breakout forming",
                   equity=1000.0, price=3.21, hwm=1000.0, context="armed from disarmed")
    journal.append(cfg.overlay.journal_path, "disarm", "disarmed", "chop ahead")
    df = journal.load(cfg.overlay.journal_path)
    assert len(df) == 2
    assert list(df.columns) == journal.COLUMNS
    assert df.iloc[0]["event"] == "arm"
    assert df.iloc[0]["note"] == "breakout forming"
    assert df.iloc[1]["state_after"] == "disarmed"


def test_load_missing_journal_is_empty(cfg):
    df = journal.load(cfg.overlay.journal_path)
    assert df.empty and list(df.columns) == journal.COLUMNS


def test_measured_break_even_from_trades(cfg):
    for pnl in (0.012, 0.009, -0.006, -0.004):
        journal.append(cfg.overlay.journal_path, "trade", "armed", "trail_stop",
                       context=f"weighted_pct={pnl}")
    be = journal.measured_break_even(journal.load(cfg.overlay.journal_path))
    # W = 0.0105, L = 0.005 -> p* = 0.005 / 0.0155
    assert be == pytest.approx(0.005 / 0.0155)


def test_measured_break_even_none_without_two_sides(cfg):
    journal.append(cfg.overlay.journal_path, "trade", "armed", "x",
                   context="weighted_pct=0.01")
    assert journal.measured_break_even(journal.load(cfg.overlay.journal_path)) is None


def test_arm_requires_note(cfg, tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--config", _config_file(cfg, tmp_path), "arm"])


def test_arm_disarm_cli_roundtrip(cfg, tmp_path, capsys):
    cfg_file = _config_file(cfg, tmp_path)
    assert cli.main(["--config", cfg_file, "arm", "--note", "breakout forming"]) == 0
    assert cli.main(["--config", cfg_file, "disarm", "--note", "chop ahead"]) == 0
    out = capsys.readouterr().out
    assert "ARMED" in out and "DISARMED" in out

    from near_bot import overlay as ov

    state = ov.load_state(cfg.overlay.state_path)
    assert state.state == ov.DISARMED
    events = journal.load(cfg.overlay.journal_path)
    assert list(events["event"]) == ["arm", "disarm"]
    assert list(events["note"]) == ["breakout forming", "chop ahead"]


def _frame_and_cfg(cfg: Config):
    bars = data.aggregate(make_1m_klines(12000), cfg.aggregate_minutes,
                          drop_incomplete=True)
    return indicators.add_all(bars, cfg.strategy)


def test_score_calls_open_and_scored(cfg):
    frame = _frame_and_cfg(cfg)
    t0 = frame["timestamp"].iloc[10]
    far_future = frame["timestamp"].iloc[-1] + pd.Timedelta(days=30)
    events = pd.DataFrame({
        "ts_utc": pd.to_datetime([t0, far_future], utc=True),
        "event": ["arm", "disarm"], "note": ["a", "b"],
        "state_after": ["armed", "disarmed"], "equity": [""] * 2,
        "price": [""] * 2, "hwm": [""] * 2, "context": [""] * 2,
    })
    scored = journal.score_calls(events, frame, cfg, horizon_days=5)
    assert list(scored["status"]) == ["scored", "open"]
    row = scored.iloc[0]
    assert row["n_bars"] == 5 * 24  # 5 days of 60m bars
    # arm is correct iff the replayed strategy made money in the window
    assert row["correct"] == bool(row["strat_ret"] > 0.0)


def test_accuracy_report_and_format(cfg):
    cfg.aggregate_minutes = 15  # 12000 1m bars -> 800 bars, room for 5d windows
    frame = _frame_and_cfg(cfg)
    ts = frame["timestamp"]
    events = pd.DataFrame({
        "ts_utc": pd.to_datetime([ts.iloc[10], ts.iloc[150], ts.iloc[300]], utc=True),
        "event": ["arm", "disarm", "arm"],
        "note": list("abc"),
        "state_after": ["armed", "disarmed", "armed"],
        "equity": [""] * 3, "price": [""] * 3, "hwm": [""] * 3,
        "context": ["weighted_pct=0.01", "", "weighted_pct=-0.005"],
    })
    # add trade events so the break-even can be measured
    trade_events = pd.DataFrame({
        "ts_utc": pd.to_datetime([ts.iloc[20], ts.iloc[30]], utc=True),
        "event": ["trade", "trade"], "note": ["x", "y"],
        "state_after": ["armed"] * 2, "equity": [""] * 2, "price": [""] * 2,
        "hwm": [""] * 2, "context": ["weighted_pct=0.01", "weighted_pct=-0.005"],
    })
    events = pd.concat([events, trade_events]).sort_values("ts_utc").reset_index(drop=True)
    scored = journal.score_calls(events, frame, cfg, horizon_days=5)
    rep = journal.accuracy_report(scored, events, n_boot=200)
    assert rep["n_calls"] == 3
    assert rep["n_scored"] == 3
    assert "overall" in rep and "verdict" in rep
    text = journal.format_report(scored, rep)
    assert "VERDICT" in text and "break-even" in text


def test_journal_report_cli(cfg, tmp_path, capsys):
    frame_csv = tmp_path / "data" / "NEARUSDT_1m_test.csv"
    data.save_csv(make_1m_klines(12000), frame_csv)
    bars = data.aggregate(data.load_csv(frame_csv), cfg.aggregate_minutes,
                          drop_incomplete=True)
    t0 = bars["timestamp"].iloc[10].isoformat()
    journal.append(cfg.overlay.journal_path, "arm", "armed", "test call")
    # backdate the call to inside the data window
    df = pd.read_csv(cfg.overlay.journal_path)
    df.loc[0, "ts_utc"] = t0
    df.to_csv(cfg.overlay.journal_path, index=False)

    cfg_file = _config_file(cfg, tmp_path)
    rc = cli.main(["--config", cfg_file, "journal", "report",
                   "--csv", str(frame_csv), "--horizon-days", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Journal report" in out and "overall" in out

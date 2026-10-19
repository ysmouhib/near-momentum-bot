"""Validation battery: statistics, BTC conditioning, report and CLI wiring.

The battery is what every future claim of this project stands on, so its
plumbing earns tests the same way the engine did: the vectorised bootstrap
must reproduce the scalar reference exactly (seeded CIs cannot drift), the
noise clause must fire on noise and stay quiet on a genuine edge, the BTC
regime label must be causal, and ``near-bot validate`` must write a report
that ends in a verdict and names any symbol it could not find.
"""

from __future__ import annotations

import copy
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from conftest import make_1m_klines

from near_bot import backtest, cli, data, indicators, stats, validate
from near_bot import walkforward as wf
from near_bot.backtest import Trade
from near_bot.config import Config


def _cfg():
    cfg = Config()
    cfg.aggregate_minutes = 15  # synthetic fixture -> enough bars for windows
    return cfg


def _bars(seed: int, n: int = 24000, cfg=None):
    cfg = cfg or _cfg()
    return data.aggregate(make_1m_klines(n, seed=seed), cfg.aggregate_minutes,
                          drop_incomplete=True)


def _trade(entry, exit_, weighted_net):
    return Trade(entry_time=entry, exit_time=exit_, entry_price=1.0, exit_price=1.0,
                 weight=1.0, reason_out="x", gross_pct=weighted_net,
                 net_pct=weighted_net, weighted_net=weighted_net, bars_held=exit_ - entry)


# ------------------------------------------------------------------ stats

def _scalar_block_resample(x, block, rng):
    """The original loop implementation, kept as the reference."""
    n = len(x)
    block = max(1, min(block, n))
    out = np.empty(n)
    filled = 0
    while filled < n:
        start = int(rng.integers(0, n))
        take = min(block, n - filled)
        out[filled:filled + take] = x[(start + np.arange(take)) % n]
        filled += take
    return out


@pytest.mark.parametrize(("n", "block"), [(100, 7), (168, 168), (1000, 168), (5, 50), (37, 1)])
def test_vectorised_block_resample_matches_scalar_reference(n, block):
    x = np.random.default_rng(0).normal(size=n)
    a = stats._block_resample(x, block, np.random.default_rng(42))
    b = _scalar_block_resample(x, block, np.random.default_rng(42))
    assert np.array_equal(a, b)


def test_ann_return_and_max_drawdown_stats():
    x = np.array([0.1, -0.5, 0.2])
    assert stats.max_drawdown_stat(x) == pytest.approx(0.55 / 1.1 - 1.0)
    # with 3 bars per year the annualised return is the total return
    bar_minutes = 365 * 24 * 60 // 3
    assert stats.ann_return_stat(bar_minutes)(x) == pytest.approx(1.1 * 0.5 * 1.2 - 1.0)
    assert stats.max_drawdown_stat(np.array([0.01, 0.02])) == 0.0


# --------------------------------------------------------------- verdicts

def test_noise_clause_fires_on_zero_mean_and_no_decisions():
    oos = np.random.default_rng(3).normal(0.0, 0.01, 2000)
    blk = validate.evaluate(oos, [], bar_minutes=60, top_k=3, n_boot=200, block=24)
    assert blk["noise"] is True
    assert blk["verdict"].startswith(validate.NOISE_SENTENCE)
    assert "too few decisions" in blk["verdict"]
    assert blk["n_eff"] == 0


def test_genuine_edge_survives_and_is_not_thin():
    rng = np.random.default_rng(4)
    oos = rng.normal(0.002, 0.005, 3000)
    trades = [_trade(20 * k, 20 * k + 10, 0.01 + rng.normal(0, 0.002))
              for k in range(150)]
    blk = validate.evaluate(oos, trades, bar_minutes=60, top_k=1, n_boot=200,
                            block=24, trial_sharpes=rng.normal(0.0, 0.01, 50))
    assert blk["noise"] is False
    assert blk["sharpe"][1] > 0 and blk["ann_return"][1] > 0
    assert blk["expectancy_per_decision"][1] > 0
    assert blk["n_eff"] == 150
    assert "provisionally established" in blk["verdict"]
    text = "\n".join(validate.format_block("X", blk))
    assert "statistically thin" not in text and "N_eff = 150" in text


def test_few_decisions_are_thin_never_established():
    """A strong per-bar edge on six decisions is thin (DESIGN.md §7.5), and
    the per-decision CI over a handful of clusters must not collapse."""
    rng = np.random.default_rng(5)
    oos = rng.normal(0.002, 0.005, 3000)
    trades = [_trade(100 * k, 100 * k + 10, v)
              for k, v in enumerate([0.01, 0.03, 0.02, 0.05, 0.015, 0.04])]
    blk = validate.evaluate(oos, trades, bar_minutes=60, top_k=1, n_boot=500,
                            block=24, trial_sharpes=rng.normal(0.0, 0.01, 50))
    assert blk["noise"] is False
    assert "statistically thin" in blk["verdict"]
    assert "established, keep measuring" not in blk["verdict"]
    _, lo, hi = blk["expectancy_per_decision"]
    assert lo < hi  # resampled per cluster, not rotated as one block
    overall = validate.verdict_lines([("v", {"symbols": {"NEARUSDT": blk}})],
                                     production="NEARUSDT")[-1]
    assert "not established" in overall and "statistically thin" in overall


def test_pooled_expectancy_clusters_across_symbols():
    """Two symbols entering on the same market move are one decision."""
    near = [_trade(10, 20, 0.01), _trade(40, 50, 0.01)]
    eth = [_trade(12, 22, 0.02), _trade(70, 80, -0.01)]
    blk = validate.evaluate(np.zeros(50), near + eth, bar_minutes=60, top_k=1,
                            n_boot=50, block=5, pnl_scale=0.5)
    assert blk["n_trades"] == 4 and blk["n_eff"] == 3


# ------------------------------------------------------------- BTC regime

def test_btc_regime_is_causal_and_labelled():
    cfg = _cfg()
    bars = _bars(seed=9)
    labels = validate.btc_regime(bars, cfg.strategy)
    assert set(labels.dropna()) <= set(validate.REGIMES)
    assert labels.iloc[: cfg.strategy.trend_window - 1].isna().all()
    k = 900
    mutated = bars.copy()
    mutated.loc[k:, "close"] *= 1.7
    after = validate.btc_regime(mutated, cfg.strategy)
    pd.testing.assert_series_equal(labels.iloc[:k], after.iloc[:k])


def test_condition_on_regime_uses_label_known_before_the_bar():
    idx = pd.date_range("2026-01-01", periods=6, freq="1h")
    regime = pd.Series(["up", "up", "down", "chop", "up", "down"], index=idx, dtype=object)
    rets = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05, 0.06], index=idx)
    out = validate.condition_on_regime(rets, regime, bar_minutes=60)
    # lagged labels: [nan, up, up, down, chop, up]
    assert out["unlabelled"] == 1
    assert out["up"]["n_bars"] == 3
    assert out["up"]["total_return"] == pytest.approx(1.02 * 1.03 * 1.06 - 1.0)
    assert out["down"]["total_return"] == pytest.approx(0.04)
    assert out["chop"]["total_return"] == pytest.approx(0.05)
    assert sum(out[r]["n_bars"] for r in validate.REGIMES) + out["unlabelled"] == 6


# ---------------------------------------------------------------- battery

def test_walkforward_exposes_window_returns_trials_and_n_eff(klines_1m):
    cfg = _cfg()
    bars = data.aggregate(klines_1m, cfg.aggregate_minutes, drop_incomplete=True)
    res = wf.run(bars, cfg, train_bars=400, test_bars=150)
    assert res.n_combos == 12
    traded = [w for w in res.windows if w.combos is not None]
    for w in traded:
        assert len(w.oos_returns) == 150
        assert w.trial_sharpes[: len(w.in_sample_sharpe)] == w.in_sample_sharpe
    if res.oos_metrics.get("n_trades", 0):
        assert f"N_eff = {res.n_eff}" in res.summary()
        assert res.n_eff <= res.oos_metrics["n_trades"]


def test_run_battery_pools_traded_symbols_and_keeps_btc_apart():
    cfg = _cfg()
    bars = {"NEARUSDT": _bars(7), "ETHUSDT": _bars(8), "BTCUSDT": _bars(9)}
    battery = validate.run_battery(bars, cfg, train_bars=384, test_bars=96,
                                   top_k=3, n_boot=50, block=24)
    assert set(battery["symbols"]) == set(bars)
    pooled = battery["pooled"]
    assert pooled is not None and pooled["members"] == ["NEARUSDT", "ETHUSDT"]
    for blk in [*battery["symbols"].values(), pooled]:
        assert "verdict" in blk and "n_eff" in blk
        assert blk["dsr_trials"] >= stats.TRIALS_TRIED
        assert "regime" in blk
    report = validate.render_report([("train 4d / test 1d", battery)],
                                    ["- header"], production="NEARUSDT")
    assert report.index("NEARUSDT (production)") < report.index("POOLED")
    assert report.index("POOLED") < report.index("BTCUSDT (context stream")
    assert "## Verdict" in report and "**Overall:" in report


def test_dsr_trials_are_measured_on_the_tested_oos_bars():
    """The deflated-Sharpe benchmark must compare like with like: every grid
    cell traded fixed over exactly the OOS bars of the tested series, N from
    the ledger (the e0d6f3c run used short in-sample window Sharpes and got an
    SR* of +6..+13 annualised)."""
    cfg = _cfg()
    bars = _bars(7)
    res = wf.run(bars, cfg, train_bars=384, test_bars=96, top_k=3)
    fixed = validate.fixed_config_returns(bars, cfg, res.oos_returns.index)
    assert len(fixed) == res.n_combos == 12
    oos_ts = validate.timestamped(res, bars).index
    for series in fixed.values():
        assert series.index.equals(oos_ts)
    # one fixed config, run directly, gives the same OOS bars
    key = tuple(sorted(next(iter(wf._combos(wf.DEFAULT_GRID))).items()))
    params = replace(cfg.strategy, **dict(key))
    sub = copy.copy(cfg)
    sub.strategy = params
    direct = backtest.run(indicators.add_all(bars, params), sub).bar_returns.to_numpy()
    assert np.array_equal(fixed[key].to_numpy(), direct[list(res.oos_returns.index)])

    battery = validate.run_battery({"NEARUSDT": bars}, cfg, train_bars=384,
                                   test_bars=96, top_k=3, n_boot=50, block=24)
    blk = battery["symbols"]["NEARUSDT"]
    assert blk["dsr_trials"] == stats.TRIALS_TRIED
    # the benchmark is on the scale of an OOS-length Sharpe, not of 384-bar noise
    sharpes = [validate._per_bar_sharpe(s) for s in fixed.values()]
    expected = stats.expected_max_sharpe(stats.TRIALS_TRIED, float(np.var(sharpes, ddof=1)))
    ann = np.sqrt(365 * 24 * 60 / cfg.aggregate_minutes)
    assert blk["dsr_benchmark"] == pytest.approx(expected * ann)


def test_run_battery_reports_short_history_instead_of_dropping_it():
    cfg = _cfg()
    bars = {"NEARUSDT": _bars(7), "SOLUSDT": _bars(8, n=3000)}
    battery = validate.run_battery(bars, cfg, train_bars=384, test_bars=96,
                                   top_k=3, n_boot=50, block=24)
    sol = battery["symbols"]["SOLUSDT"]
    assert sol["noise"] is True and "Not enough history" in sol["verdict"]
    assert battery["pooled"] is None  # one traded symbol is not a pool


def test_locate_csvs_enforces_one_window(tmp_path):
    for name in ("NEARUSDT_1m_20230101_20250101.csv", "NEARUSDT_1m_20230101_20260101.csv",
                 "ETHUSDT_1m_20230101_20250101.csv", "BTCUSDT_1m_20230101_20260101.csv"):
        (tmp_path / name).write_text("")
    tag, found, missing = validate.locate_csvs(
        ["NEARUSDT", "ETHUSDT", "BTCUSDT"], tmp_path, "2023-01-01")
    assert tag == "20230101_20260101"  # newest window of the first symbol
    assert set(found) == {"NEARUSDT", "BTCUSDT"}
    assert set(missing) == {"ETHUSDT"}  # only has a different window
    tag, found, _ = validate.locate_csvs(
        ["NEARUSDT", "ETHUSDT"], tmp_path, "2023-01-01", "2025-01-01")
    assert tag == "20230101_20250101" and set(found) == {"NEARUSDT", "ETHUSDT"}


# -------------------------------------------------------------------- CLI

def test_validate_cli_writes_report_with_verdict(tmp_path, capsys):
    import yaml

    data_dir = tmp_path / "data"
    for sym, seed in (("NEARUSDT", 7), ("BTCUSDT", 9)):
        data.save_csv(make_1m_klines(24000, seed=seed),
                      data_dir / f"{sym}_1m_20260101_20260117.csv")
    cfg = _cfg()
    cfg.data.dir = str(data_dir)
    cfg.data.start = "2026-01-01"
    cfg.data.symbols = ["NEARUSDT", "ETHUSDT", "BTCUSDT"]
    cfg_file = tmp_path / "config.yaml"
    with cfg_file.open("w") as fh:
        yaml.safe_dump(cfg.to_dict(), fh)

    out = tmp_path / "reports" / "validate.md"
    rc = cli.main(["--config", str(cfg_file), "validate", "--windows", "4:1",
                   "--n-boot", "50", "--block", "24", "--out", str(out), "--no-plots"])
    assert rc == 0
    report = out.read_text(encoding="utf-8")
    assert "## Window variant: train 4d / test 1d" in report
    assert "`20260101_20260117`" in report
    assert "independent decisions N_eff" in report
    assert "Missing data (skipped, not silently): ETHUSDT" in report
    assert "BTCUSDT (context stream" in report
    assert report.rstrip().endswith("Not financial advice.")
    stdout = capsys.readouterr().out
    assert "## Verdict" in stdout and "**Overall:" in stdout
    assert "Missing data (skipped): ETHUSDT" in stdout


def test_validate_cli_fails_loudly_without_data(tmp_path, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("data:\n  dir: " + str(tmp_path / "empty").replace("\\", "/") + "\n")
    assert cli.main(["--config", str(cfg_file), "validate", "--no-plots"]) == 1
    assert "near-bot download" in capsys.readouterr().err


def test_window_spec_parsing():
    import argparse

    assert cli._window_spec("90:30") == (90, 30)
    for bad in ("90", "90:0", "a:b"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._window_spec(bad)

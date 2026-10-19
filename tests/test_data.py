"""Data-layer tests: aggregation math, incomplete-bar handling, timestamps."""

from __future__ import annotations

import zipfile

import numpy as np
import pandas as pd

from near_bot import data


def test_aggregate_math(klines_1m):
    bars = data.aggregate(klines_1m, 60, drop_incomplete=False)
    first = klines_1m.iloc[:60]
    b0 = bars.iloc[0]
    assert b0["open"] == first["open"].iloc[0]
    assert b0["close"] == first["close"].iloc[-1]
    assert b0["high"] == first["high"].max()
    assert b0["low"] == first["low"].min()
    assert np.isclose(b0["volume"], first["volume"].sum())
    assert np.isclose(b0["taker_buy_volume"], first["taker_buy_base"].sum())


def test_aggregate_drops_incomplete_tail():
    n = 130  # 2 full hours + 10 minutes
    df = pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=n, freq="1min"),
        "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
        "volume": 10.0, "quote_volume": 10.5, "trades": 5,
        "taker_buy_base": 6.0, "taker_buy_quote": 6.3,
    })
    bars = data.aggregate(df, 60, drop_incomplete=True)
    assert len(bars) == 2
    bars2 = data.aggregate(df, 60, drop_incomplete=False)
    assert len(bars2) == 3


def test_microsecond_timestamps_detected():
    us = pd.Series([1784073600000000, 1784073660000000])  # µs
    ms = pd.Series([1784073600000, 1784073660000])          # ms
    assert data._to_datetime(us).iloc[0] == data._to_datetime(ms).iloc[0]


def test_read_dump_zip_roundtrip(klines_1m, tmp_path):
    # dumps store timestamps as epoch (ms or µs), not ISO strings
    raw = klines_1m.copy()
    raw["open_time"] = (
        (raw["open_time"] - pd.Timestamp("1970-01-01")) // pd.Timedelta("1ms")
    )
    csv_bytes = raw.to_csv(index=False, header=False).encode()
    zp = tmp_path / "NEARUSDT-1m-2026-01-01.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("NEARUSDT-1m-2026-01-01.csv", csv_bytes)
    df = data.read_dump_zip(zp)
    assert len(df) == len(klines_1m)
    assert df["open_time"].iloc[0] == klines_1m["open_time"].iloc[0]
    assert np.isclose(df["close"].iloc[-1], klines_1m["close"].iloc[-1])


def test_csv_roundtrip(klines_1m, tmp_path):
    p = tmp_path / "k.csv"
    data.save_csv(klines_1m, p)
    back = data.load_csv(p)
    assert len(back) == len(klines_1m)
    assert pd.api.types.is_datetime64_any_dtype(back["open_time"])


def test_find_gaps_detects_hole():
    ot = pd.date_range("2026-01-01", periods=100, freq="1min")
    df = pd.DataFrame({"open_time": ot.delete(range(40, 50))})  # 10 missing minutes
    gaps = data.find_gaps(df)
    assert len(gaps) == 1
    gap_start, gap_end, n_missing = gaps[0]
    assert n_missing == 10
    assert gap_start == ot[40]
    assert gap_end == ot[49]


def test_find_gaps_ignores_gaps_within_tolerance():
    ot = pd.date_range("2026-01-01", periods=100, freq="1min")
    # exactly 5 missing minutes == the default tolerance: not reported
    df = pd.DataFrame({"open_time": ot.delete(range(10, 15))})
    assert data.find_gaps(df) == []
    # ...but it is reported once the tolerance drops below it
    assert len(data.find_gaps(df, max_gap_minutes=4)) == 1


def test_find_gaps_handles_empty_and_single_row():
    assert data.find_gaps(pd.DataFrame({"open_time": []})) == []
    one = pd.DataFrame({"open_time": pd.date_range("2026-01-01", periods=1, freq="1min")})
    assert data.find_gaps(one) == []


def test_find_gaps_unsorted_input():
    ot = pd.date_range("2026-01-01", periods=100, freq="1min")
    df = pd.DataFrame({"open_time": ot.delete(range(40, 50))})
    shuffled = df.sample(frac=1, random_state=1).reset_index(drop=True)
    gaps = data.find_gaps(shuffled)
    assert len(gaps) == 1 and gaps[0][2] == 10


def test_window_gaps_covers_window_edges():
    # only Jan 10 present inside a [Jan 1, Jan 12) window
    df = pd.DataFrame({"open_time": pd.date_range("2026-01-10", periods=1440, freq="1min")})
    gaps = data.window_gaps(df, "2026-01-01", "2026-01-12")
    assert len(gaps) == 2
    assert gaps[0] == (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-09 23:59"), 9 * 1440)
    assert gaps[1] == (pd.Timestamp("2026-01-11"), pd.Timestamp("2026-01-11 23:59"), 1440)


def test_window_gaps_full_coverage_is_clean():
    ot = pd.date_range("2026-01-01", periods=3 * 1440, freq="1min")
    df = pd.DataFrame({"open_time": ot})
    assert data.window_gaps(df, "2026-01-01", "2026-01-04") == []
    assert data.window_gaps(df.iloc[:0], "2026-01-01", "2026-01-04") == []

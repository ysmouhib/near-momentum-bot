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

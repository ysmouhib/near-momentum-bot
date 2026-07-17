"""Market data: Binance kline parsing, N-minute aggregation, dump loading.

Two ways to get 1-minute klines without an API key:

1. The REST API (``scripts/download_data.py``) — simple, but api.binance.com
   is geo-blocked in some regions.
2. The public data dumps at ``data.binance.vision`` — daily/monthly zips of
   CSVs, served from a CDN that works where the API does not. This module
   reads those zips directly.

Aggregation detail that matters: the *most recent* aggregated bar is usually
incomplete (fewer than N one-minute bars). Trading on an unfinished bar is a
classic look-ahead bug, so aggregation can drop it.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd

# Column layout of a Binance kline (REST /api/v3/klines and dump CSVs alike).
_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades", "taker_buy_base",
    "taker_buy_quote", "ignore",
]

_NUMERIC = ["open", "high", "low", "close", "volume", "quote_volume",
            "taker_buy_base", "taker_buy_quote"]


def _to_datetime(ms_or_us: pd.Series) -> pd.Series:
    """Binance switched dump timestamps from ms to µs; accept either."""
    unit = "us" if int(ms_or_us.iloc[0]) > 10**14 else "ms"
    return pd.to_datetime(ms_or_us, unit=unit)


def klines_to_df(klines: list) -> pd.DataFrame:
    """Convert raw Binance klines (list of lists) into a typed DataFrame."""
    df = pd.DataFrame(klines, columns=_KLINE_COLUMNS)
    for col in _NUMERIC:
        df[col] = pd.to_numeric(df[col])
    df["open_time"] = _to_datetime(df["open_time"])
    df["trades"] = pd.to_numeric(df["trades"])
    return df


def read_dump_zip(path: str | Path) -> pd.DataFrame:
    """Read one daily/monthly kline zip from data.binance.vision."""
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            df = pd.read_csv(io.BytesIO(fh.read()), header=None, names=_KLINE_COLUMNS)
    for col in _NUMERIC:
        df[col] = pd.to_numeric(df[col])
    df["open_time"] = _to_datetime(df["open_time"])
    df["trades"] = pd.to_numeric(df["trades"])
    return df


def load_dump_dir(directory: str | Path, symbol: str) -> pd.DataFrame:
    """Concatenate every ``<symbol>-1m-*.zip`` dump in a directory."""
    paths = sorted(Path(directory).glob(f"{symbol}-1m-*.zip"))
    if not paths:
        raise FileNotFoundError(f"No {symbol}-1m-*.zip dumps in {directory}")
    df = pd.concat([read_dump_zip(p) for p in paths], ignore_index=True)
    df = df.drop_duplicates("open_time").sort_values("open_time")
    return df.reset_index(drop=True)


def aggregate(df_1m: pd.DataFrame, minutes: int, drop_incomplete: bool = True) -> pd.DataFrame:
    """Roll 1-minute bars up into ``minutes``-minute bars.

    Returns columns: timestamp, open, high, low, close, volume, quote_volume,
    trades, taker_buy_volume. If ``drop_incomplete`` is True, the trailing
    bucket with fewer than ``minutes`` sub-bars is removed.
    """
    df = df_1m.copy()
    df["_bucket"] = df["open_time"].dt.floor(f"{minutes}min")

    agg = (
        df.groupby("_bucket")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            quote_volume=("quote_volume", "sum"),
            trades=("trades", "sum"),
            taker_buy_volume=("taker_buy_base", "sum"),
            _count=("open", "size"),
        )
        .reset_index()
        .rename(columns={"_bucket": "timestamp"})
    )

    if drop_incomplete and len(agg) > 0 and agg.iloc[-1]["_count"] < minutes:
        agg = agg.iloc[:-1]

    return agg.drop(columns="_count").reset_index(drop=True)


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load a saved 1-minute kline CSV (as written by scripts/download_data.py)."""
    df = pd.read_csv(path)
    if "open_time" in df.columns:
        df["open_time"] = pd.to_datetime(df["open_time"])
    return df


def save_csv(df: pd.DataFrame, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)

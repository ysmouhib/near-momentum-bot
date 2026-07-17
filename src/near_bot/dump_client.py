"""Client for Binance's public data dumps at data.binance.vision.

These are daily/monthly zips of kline CSVs served from a CDN — no API key,
and reachable from regions where api.binance.com is blocked (and from CI
sandboxes). Used by ``near-bot download --source dumps`` and the tests' own
data refresh path.
"""

from __future__ import annotations

import datetime as dt
import io
import time
import zipfile

import pandas as pd
import requests

from .data import _KLINE_COLUMNS, _NUMERIC, _to_datetime

BASE = "https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{fname}"


def fetch_daily(symbol: str, days: int, pause: float = 0.1) -> pd.DataFrame:
    """Download the last ``days`` days of 1m klines for ``symbol``."""
    frames = []
    today = dt.date.today()
    for i in range(days, 0, -1):
        day = (today - dt.timedelta(days=i)).isoformat()
        fname = f"{symbol}-1m-{day}.zip"
        url = BASE.format(symbol=symbol, fname=fname)
        resp = requests.get(url, timeout=30)
        if resp.status_code == 404:
            continue  # day not published yet (or symbol typo)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            with z.open(z.namelist()[0]) as fh:
                frames.append(pd.read_csv(fh, header=None, names=_KLINE_COLUMNS))
        time.sleep(pause)
    if not frames:
        raise RuntimeError(f"No dump data returned for {symbol}")
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates("open_time").sort_values("open_time")
    for col in _NUMERIC:
        df[col] = pd.to_numeric(df[col])
    df["open_time"] = _to_datetime(df["open_time"])
    df["trades"] = pd.to_numeric(df["trades"])
    return df.reset_index(drop=True)

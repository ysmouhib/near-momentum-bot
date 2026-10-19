"""Client for Binance's public data dumps at data.binance.vision.

These are daily/monthly zips of kline CSVs served from a CDN — no API key,
and reachable from regions where api.binance.com is blocked (and from CI
sandboxes). Used by ``near-bot download --source dumps`` and the tests' own
data refresh path.

Multi-year pulls use the *monthly* zips for every calendar month fully
inside the requested window (~36 requests for a 3-year pull instead of
~1100 daily ones — fewer requests, fewer chances for a flaky CDN to ruin a
run), with daily zips only for the partial leading/trailing month. Every
fetched file is hashed and recorded in a manifest, because over a 3-year
window a missing day is a *when*, not an *if*: the old ``fetch_daily``
swallowed 404s with ``continue``, which turns exchange incidents and
late-published corrections into silent holes in the data. ``fetch_range``
never skips silently — a missing day lands in ``missing_days``, a missing
monthly zip falls back to that month's daily zips and is recorded in
``missing_months``, and when the published ``.CHECKSUM`` sidecar exists the
zip's SHA-256 is verified before parsing (a corrupt CSV inside a valid zip
would otherwise parse fine and poison every downstream number).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import time
import zipfile

import pandas as pd
import requests

from .data import _KLINE_COLUMNS, _NUMERIC, _to_datetime

BASE = "https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{fname}"
MONTHLY_BASE = "https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{fname}"


def _zip_bytes_to_df(content: bytes) -> pd.DataFrame:
    """Parse the CSV inside one dump zip into a typed kline DataFrame."""
    with zipfile.ZipFile(io.BytesIO(content)) as z, z.open(z.namelist()[0]) as fh:
        df = pd.read_csv(fh, header=None, names=_KLINE_COLUMNS)
    for col in _NUMERIC:
        df[col] = pd.to_numeric(df[col])
    df["open_time"] = _to_datetime(df["open_time"])
    df["trades"] = pd.to_numeric(df["trades"])
    return df


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
        frames.append(_zip_bytes_to_df(resp.content))
        time.sleep(pause)
    if not frames:
        raise RuntimeError(f"No dump data returned for {symbol}")
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates("open_time").sort_values("open_time")
    return df.reset_index(drop=True)


def _first_of_next_month(d: dt.date) -> dt.date:
    return (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


def _window_plan(start: dt.date, end: dt.date) -> list[tuple[str, dt.date]]:
    """Split [start, end) into monthly and daily dump files.

    A calendar month becomes a ("monthly", month-start) entry when it lies
    fully inside the window; the ragged leading/trailing month days become
    ("daily", day) entries.
    """
    plan: list[tuple[str, dt.date]] = []
    month = start.replace(day=1)
    while month < end:
        nxt = _first_of_next_month(month)
        if month >= start and nxt <= end:
            plan.append(("monthly", month))
        else:
            day = max(start, month)
            while day < min(end, nxt):
                plan.append(("daily", day))
                day += dt.timedelta(days=1)
        month = nxt
    return plan


def _fetch_file(url: str, fname: str, source: str, verify_checksum: bool,
                manifest: dict) -> pd.DataFrame | None:
    """GET one dump zip, verify and parse it, record it in the manifest.

    Returns None on 404 (the caller decides whether that means a missing
    day or a monthly zip to fall back from). Checksum verification happens
    *before* parsing: the manifest's sha256 is only trustworthy if it was
    checked against the publisher's sidecar when one exists.
    """
    resp = requests.get(url, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    digest = hashlib.sha256(resp.content).hexdigest()
    verified = None
    if verify_checksum:
        chk = requests.get(url + ".CHECKSUM", timeout=30)
        if chk.status_code == 404:
            verified = False  # no sidecar published: hash recorded, unverified
        else:
            chk.raise_for_status()
            if chk.text.split()[0].lower() != digest:
                raise RuntimeError(f"SHA-256 mismatch for {fname}: corrupt download")
            verified = True
    df = _zip_bytes_to_df(resp.content)
    entry = {"name": fname, "source": source, "rows": len(df), "sha256": digest}
    if verified is not None:
        entry["sha256_verified"] = verified
    manifest["files"].append(entry)
    return df


def _published(symbol: str, day: dt.date) -> bool:
    """Cheap existence probe for one daily dump (its tiny .CHECKSUM sidecar)."""
    fname = f"{symbol}-1m-{day.isoformat()}.zip"
    resp = requests.get(BASE.format(symbol=symbol, fname=fname) + ".CHECKSUM", timeout=30)
    return resp.status_code != 404


def fetch_range(symbol: str, start: dt.date, end: dt.date, pause: float = 0.1,
                verify_checksum: bool = True,
                from_listing: bool = False) -> tuple[pd.DataFrame, dict]:
    """Download 1m klines for the fixed window [start, end) (end exclusive).

    Monthly zips cover every calendar month fully inside the window; daily
    zips cover the partial leading/trailing month days and stand in for any
    monthly zip that 404s. Returns the concatenated DataFrame plus a
    manifest dict (``symbol``, ``start``, ``end``, ``files`` with
    name/source/rows/sha256 per file, ``missing_days``, ``missing_months``,
    ``pre_listing``) that makes the dataset auditable and re-downloadable.

    ``from_listing`` handles a symbol listed *after* ``start``: files absent
    before the first published one are recorded under ``pre_listing`` (and
    ``listing_start`` = the first bar) instead of as missing data — a
    listing date is not a hole. A missing monthly zip before any data is
    probed once on its last day: nothing there means the whole month predates
    the listing; something there means the listing fell inside the month, so
    its daily zips are fetched as usual. After the first published file,
    every 404 is a real gap again and is reported as such.
    """
    if end <= start:
        raise ValueError(f"Empty window: start={start} end={end}")
    manifest: dict = {"symbol": symbol, "start": start.isoformat(),
                      "end": end.isoformat(), "files": [],
                      "missing_days": [], "missing_months": [], "pre_listing": []}
    frames: list[pd.DataFrame] = []
    plan = _window_plan(start, end)
    i = 0
    while i < len(plan):
        kind, when = plan[i]
        if kind == "monthly":
            fname = f"{symbol}-1m-{when:%Y-%m}.zip"
            url = MONTHLY_BASE.format(symbol=symbol, fname=fname)
            df = _fetch_file(url, fname, "monthly", verify_checksum, manifest)
            if df is None:
                last_day = min(_first_of_next_month(when), end) - dt.timedelta(days=1)
                if from_listing and not frames and not _published(symbol, last_day):
                    manifest["pre_listing"].append(f"{when:%Y-%m}")
                    i += 1
                    continue
                manifest["missing_months"].append(f"{when:%Y-%m}")
                # Splice this month's daily zips into the plan, in place.
                days = []
                day = max(when, start)
                stop = min(_first_of_next_month(when), end)
                while day < stop:
                    days.append(("daily", day))
                    day += dt.timedelta(days=1)
                plan[i:i + 1] = days
                continue
            frames.append(df)
            time.sleep(pause)
        else:
            fname = f"{symbol}-1m-{when.isoformat()}.zip"
            url = BASE.format(symbol=symbol, fname=fname)
            df = _fetch_file(url, fname, "daily", verify_checksum, manifest)
            if df is None:
                key = "pre_listing" if from_listing and not frames else "missing_days"
                manifest[key].append(when.isoformat())
            else:
                frames.append(df)
                time.sleep(pause)
        i += 1
    if not frames:
        raise RuntimeError(f"No dump data returned for {symbol}")
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    if manifest["pre_listing"]:
        manifest["listing_start"] = df["open_time"].iloc[0].isoformat()
    return df, manifest

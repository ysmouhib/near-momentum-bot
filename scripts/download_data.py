#!/usr/bin/env python3
"""Download historical 1-minute klines over a fixed dated window.

Fixed windows are the whole point. The old ``--days N`` rolling download
walked back from *today*, so every run produced a different dataset and
"the same backtest" quietly changed underneath you from one day to the
next. With an explicit ``--start``/``--end`` window, the same command
produces the same bytes, the same bars, and the same numbers — and the
sidecar manifest records every source file (name, sha256, row count) and
every missing day, so the dataset stays auditable and re-downloadable
months later.

Two sources:

* ``dumps`` (default): Binance's public data archive at data.binance.vision —
  monthly zips for calendar months fully inside the window, daily zips for
  the ragged edges, served from a CDN that works where api.binance.com is
  geo-blocked. Writes a ``.manifest.json`` next to each CSV.
* ``api``: the REST market-data endpoint, paginated (no manifest written).

Usage:
    python scripts/download_data.py --start 2023-01-01 --end 2026-01-01
    python scripts/download_data.py --symbols NEARUSDT ETHUSDT --start 2023-01-01
        # --end omitted = latest complete UTC day (exclusive)

After writing each CSV the script checks minute coverage: gaps over 5
minutes are printed, and a gap over 1 day fails the run (exit 1) unless
``--allow-gaps`` is passed — silence about holes is what we are removing,
not the holes themselves.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd  # noqa: E402

from near_bot import data  # noqa: E402
from near_bot.dump_client import fetch_range  # noqa: E402


def _day_ms(d: dt.date) -> int:
    return int(dt.datetime.combine(d, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000)


def fetch_api(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Paginated REST klines for [start_ms, end_ms) — the --source api path."""
    import requests

    base = "https://api.binance.com/api/v3/klines"
    cursor = start_ms
    rows = []
    while cursor < end_ms:
        resp = requests.get(
            base,
            params={"symbol": symbol, "interval": "1m",
                    "startTime": cursor, "endTime": end_ms, "limit": 1000},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][6] + 1
        time.sleep(0.2)
        print(f"\r  {len(rows):,} bars", end="", flush=True)
    print()
    if not rows:
        return pd.DataFrame()
    return data.klines_to_df(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Download public Binance 1m klines over a fixed dated window")
    ap.add_argument("--symbols", nargs="+", default=["NEARUSDT"])
    ap.add_argument("--start", required=True, metavar="YYYY-MM-DD",
                    help="window start, inclusive")
    ap.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                    help="window end, exclusive; default: latest complete UTC day")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--source", choices=["dumps", "api"], default="dumps")
    ap.add_argument("--allow-gaps", action="store_true",
                    help="accept gaps longer than 1 day instead of failing")
    args = ap.parse_args(argv)

    start = dt.date.fromisoformat(args.start)
    end = (dt.date.fromisoformat(args.end) if args.end
           else dt.datetime.now(dt.timezone.utc).date())
    if end <= start:
        print(f"Empty window: start={start} end={end}", file=sys.stderr)
        return 2
    tag = f"{start:%Y%m%d}_{end:%Y%m%d}"
    rc = 0
    for symbol in args.symbols:
        print(f"Fetching {symbol} 1m klines [{start}, {end}) via {args.source} ...")
        manifest = None
        if args.source == "dumps":
            df, manifest = fetch_range(symbol, start, end)
        else:
            df = fetch_api(symbol, _day_ms(start), _day_ms(end))
        if df.empty:
            print(f"{symbol}: no data returned.", file=sys.stderr)
            rc = 1
            continue
        out = Path(args.out_dir) / f"{symbol}_1m_{tag}.csv"
        data.save_csv(df, out)
        print(f"Saved {len(df):,} rows to {out}")
        if manifest is not None:
            mpath = out.with_suffix(".manifest.json")
            with mpath.open("w") as fh:
                json.dump(manifest, fh, indent=2)
            print(f"Manifest: {mpath} ({len(manifest['files'])} files, "
                  f"{len(manifest['missing_days'])} missing days, "
                  f"{len(manifest['missing_months'])} missing months)")
        gaps = data.window_gaps(df, start, end)
        if gaps:
            print(f"{symbol}: {len(gaps)} gap(s) over 5 minutes:")
            for gap_start, gap_end, n in gaps:
                print(f"  {gap_start} .. {gap_end}  ({n:,} missing bars)")
        else:
            print(f"{symbol}: coverage OK (no gap over 5 minutes).")
        big = [g for g in gaps if g[2] > 1440]
        if big and not args.allow_gaps:
            print(f"{symbol}: {len(big)} gap(s) exceed 1 day -- pass --allow-gaps to accept.",
                  file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

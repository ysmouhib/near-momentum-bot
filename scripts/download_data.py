#!/usr/bin/env python3
"""Download historical 1-minute klines — no API key required.

Two sources:

* ``dumps`` (default): Binance's public data archive at data.binance.vision —
  daily zips of CSVs served from a CDN. Works where api.binance.com is
  geo-blocked (and in locked-down CI sandboxes).
* ``api``: the REST market-data endpoint, paginated.

Usage:
    python scripts/download_data.py --symbol NEARUSDT --days 90
    python scripts/download_data.py --symbol NEARUSDT --days 7 --source api
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from near_bot import data  # noqa: E402
from near_bot.dump_client import fetch_daily  # noqa: E402


def fetch_api(symbol: str, days: int) -> "object":
    import requests

    base = "https://api.binance.com/api/v3/klines"
    end = int(time.time() * 1000)
    cursor = end - days * 24 * 60 * 60 * 1000
    rows = []
    while cursor < end:
        resp = requests.get(
            base,
            params={"symbol": symbol, "interval": "1m",
                    "startTime": cursor, "endTime": end, "limit": 1000},
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
    return data.klines_to_df(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download public Binance 1m klines")
    ap.add_argument("--symbol", default="NEARUSDT")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--source", choices=["dumps", "api"], default="dumps")
    ap.add_argument("--out", default="data/klines_1m.csv")
    args = ap.parse_args(argv)

    print(f"Fetching {args.days}d of {args.symbol} 1m klines via {args.source} ...")
    if args.source == "dumps":
        df = fetch_daily(args.symbol, args.days)
    else:
        df = fetch_api(args.symbol, args.days)
    if df.empty:
        print("No data returned.", file=sys.stderr)
        return 1
    data.save_csv(df, args.out)
    print(f"Saved {len(df):,} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Download historical 1-minute klines over a fixed dated window.

Fixed windows are the whole point. The old ``--days N`` rolling download
walked back from *today*, so every run produced a different dataset and
"the same backtest" quietly changed underneath you from one day to the
next. With an explicit ``--start``/``--end`` window, the same command
produces the same bytes, the same bars, and the same numbers — and the
sidecar manifest records every source file (name, sha256, row count), every
missing day and every gap, so the dataset stays auditable and
re-downloadable months later.

Two sources:

* ``dumps`` (default): Binance's public data archive at data.binance.vision —
  monthly zips for calendar months fully inside the window, daily zips for
  the ragged edges, each verified against its published ``.CHECKSUM``,
  served from a CDN that works where api.binance.com is geo-blocked. Writes
  a ``.manifest.json`` next to each CSV.
* ``api``: the REST market-data endpoint, paginated (no manifest written).

Usage:
    python scripts/download_data.py --symbols NEARUSDT ETHUSDT SOLUSDT BTCUSDT \\
        --start 2021-01-01 --end 2026-10-01 --from-listing
        # --end is exclusive; omitted = latest complete UTC day

Fail loud (docs/DESIGN.md §3.3, implemented in ``near_bot.download``): gaps
over 5 minutes are printed and written into the manifest, a gap over 1 day
fails the run (exit 1) unless allow-listed in the manifest's
``allowed_gaps`` or ``--allow-gaps`` is passed, and a re-run that finds a
file republished with a different SHA-256 says so and fails. ``--from-listing``
starts a symbol listed after ``--start`` at its first published bar, and
records that in the manifest instead of failing on the pre-listing "gap".
This script and ``near-bot download`` are the same command.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from near_bot import download  # noqa: E402


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
    ap.add_argument("--from-listing", action="store_true",
                    help="a symbol listed after --start begins at its first published "
                         "bar (recorded in the manifest) instead of failing")
    args = ap.parse_args(argv)

    start = dt.date.fromisoformat(args.start)
    end = (dt.date.fromisoformat(args.end) if args.end
           else dt.datetime.now(dt.timezone.utc).date())
    return download.run(args.symbols, start, end, args.out_dir, source=args.source,
                        allow_gaps=args.allow_gaps, from_listing=args.from_listing)


if __name__ == "__main__":
    raise SystemExit(main())

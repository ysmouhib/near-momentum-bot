"""Fixed-window downloads: fetch, verify, write — and never be quiet about holes.

Why one module behind two entry points
--------------------------------------
``scripts/download_data.py`` and ``near-bot download`` are the same command
with two front doors (docs/DESIGN.md §3.1). Two copies of the per-symbol
logic is how they drift apart, so both call :func:`run` here.

What "fail loud" means in practice (DESIGN.md §3.3)
---------------------------------------------------
* Every file fetched is hashed into a manifest written next to the CSV, and
  checked against the publisher's ``.CHECKSUM`` sidecar (``dump_client``).
* Coverage is checked minute by minute over the requested window. Gaps over
  5 minutes are printed *and written into the manifest*; a gap over one day
  fails the download unless ``--allow-gaps`` — or unless the manifest's
  ``allowed_gaps`` lists it explicitly (known exchange maintenance, with a
  reason, curated by a human). Silence is what is removed, not gaps.
* Re-running the same command re-checks the previous manifest: a file whose
  SHA-256 changed (Binance republished a correction) is named and fails the
  run, and the new manifest records it under ``republished``. A changed
  input is never absorbed quietly into "the same" dataset.
* A symbol listed after the window start (``from_listing``) begins at its
  first published bar, and the manifest says so (``listing_start``,
  ``pre_listing``) — a shorter history is visible, never silent.

The manifest carries no generation timestamp on purpose: same command, same
bytes — the manifest included.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import pandas as pd

from . import data, dump_client

GAP_FAIL_BARS = 1440  # a hole longer than one day of 1m bars fails the download


def _day_ms(d: dt.date) -> int:
    return int(dt.datetime.combine(d, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000)


def fetch_api_window(symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Paginated /api/v3/klines over [start, end) — the ``--source api`` path.

    The dumps path is preferred: the REST endpoint is geo-blocked in some
    regions and needs one request per 1000 minutes. No manifest in this mode.
    """
    import requests

    end_ms = _day_ms(end)
    cursor, rows = _day_ms(start), []
    while cursor < end_ms:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
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
    if not rows:
        return pd.DataFrame()
    return data.klines_to_df(rows)


def republished(previous: dict | None, current: dict) -> list[str]:
    """Files present in both manifests whose SHA-256 changed in between."""
    if not previous:
        return []
    old = {f["name"]: f["sha256"] for f in previous.get("files", [])}
    return sorted(f["name"] for f in current["files"]
                  if f["name"] in old and old[f["name"]] != f["sha256"])


def _allowed(gap_start, gap_end, allow_list) -> bool:
    return any(pd.Timestamp(a["start"]) <= gap_start and gap_end <= pd.Timestamp(a["end"])
               for a in allow_list)


def download_symbol(symbol: str, start: dt.date, end: dt.date, out_dir,
                    source: str = "dumps", allow_gaps: bool = False,
                    from_listing: bool = False, pause: float = 0.1) -> int:
    """Fetch one symbol's window, write CSV + manifest, check coverage. Returns rc."""
    print(f"Fetching {symbol} 1m klines [{start}, {end}) via {source} ...")
    manifest = None
    try:
        if source == "dumps":
            df, manifest = dump_client.fetch_range(symbol, start, end, pause=pause,
                                                   from_listing=from_listing)
        else:
            df = fetch_api_window(symbol, start, end)
    except RuntimeError as exc:  # no data at all, or a checksum mismatch
        print(f"{symbol}: FAILED - {exc}", file=sys.stderr)
        return 1
    if df.empty:
        print(f"{symbol}: no data returned.", file=sys.stderr)
        return 1

    out = Path(out_dir) / f"{symbol}_1m_{start:%Y%m%d}_{end:%Y%m%d}.csv"
    data.save_csv(df, out)
    print(f"Saved {len(df):,} 1m klines to {out}")

    rc = 0
    window_start = pd.Timestamp(start)
    if manifest is not None and manifest.get("listing_start"):
        window_start = pd.Timestamp(manifest["listing_start"])
        print(f"{symbol}: listed after the window start - history begins "
              f"{manifest['listing_start']} ({len(manifest['pre_listing'])} "
              f"pre-listing files skipped)")
    mpath = out.with_suffix(".manifest.json")
    previous = json.loads(mpath.read_text()) if mpath.exists() else None
    allow_list = (previous or {}).get("allowed_gaps", [])

    gaps = data.window_gaps(df, window_start, end)
    gap_rows = [{"start": str(s), "end": str(e), "missing_bars": n,
                 "allowed": _allowed(s, e, allow_list)} for s, e, n in gaps]
    if gap_rows:
        print(f"{symbol}: {len(gap_rows)} gap(s) over 5 minutes:")
        for g in gap_rows:
            print(f"  {g['start']} .. {g['end']}  ({g['missing_bars']:,} missing bars)"
                  f"{'  [allow-listed]' if g['allowed'] else ''}")
    else:
        print(f"{symbol}: coverage OK (no gap over 5 minutes).")
    big = [g for g in gap_rows if g["missing_bars"] > GAP_FAIL_BARS and not g["allowed"]]
    if big and not allow_gaps:
        print(f"{symbol}: {len(big)} gap(s) exceed 1 day -- dataset incomplete (allow-list "
              f"them in the manifest's allowed_gaps with a reason, or pass --allow-gaps).",
              file=sys.stderr)
        rc = 1

    if manifest is not None:
        changed = republished(previous, manifest)
        manifest.update(rows=len(df), first_bar=str(df["open_time"].iloc[0]),
                        last_bar=str(df["open_time"].iloc[-1]), gaps=gap_rows,
                        allowed_gaps=allow_list, republished=changed)
        if changed:
            print(f"{symbol}: {len(changed)} file(s) REPUBLISHED since the previous "
                  f"manifest (SHA-256 changed): {', '.join(changed)}", file=sys.stderr)
            rc = 1
        with mpath.open("w") as fh:
            json.dump(manifest, fh, indent=2)
        print(f"Manifest: {mpath} ({len(manifest['files'])} files, "
              f"{len(manifest['missing_days'])} missing days, "
              f"{len(manifest['missing_months'])} missing monthly zips)")
    return rc


def run(symbols, start: dt.date, end: dt.date, out_dir, source: str = "dumps",
        allow_gaps: bool = False, from_listing: bool = False, pause: float = 0.1) -> int:
    """Download every symbol over one fixed window. Returns the worst rc."""
    if end <= start:
        print(f"Empty window: start={start} end={end}", file=sys.stderr)
        return 2
    rc = 0
    for symbol in symbols:
        rc = max(rc, download_symbol(symbol, start, end, out_dir, source=source,
                                     allow_gaps=allow_gaps, from_listing=from_listing,
                                     pause=pause))
    return rc

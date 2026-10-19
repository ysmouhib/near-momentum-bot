"""Offline tests for dump_client.fetch_range and the config ``data:`` section.

The Binance dump server is faked with in-memory zips — built exactly like
the real ones (headerless 12-column CSV inside a zip, ms epoch timestamps)
plus ``.CHECKSUM`` sidecars — served through a monkeypatched
``requests.get``. No network, no credentials.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pandas as pd
import pytest

from near_bot import data, dump_client
from near_bot.config import Config

SYMBOL = "NEARUSDT"
START = dt.date(2023, 1, 15)
END = dt.date(2023, 4, 10)  # exclusive
N_DAYS = (END - START).days  # 85


def _day_frame(day: dt.date) -> pd.DataFrame:
    """One full day of synthetic 1m klines in raw dump layout (ms epoch)."""
    ot = pd.date_range(pd.Timestamp(day), periods=1440, freq="1min")
    ms = (ot - pd.Timestamp("1970-01-01")) // pd.Timedelta("1ms")
    return pd.DataFrame(
        {
            "open_time": ms,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.05,
            "volume": 10.0,
            "close_time": ms + 59_999,
            "quote_volume": 10.5,
            "trades": 5,
            "taker_buy_base": 6.0,
            "taker_buy_quote": 6.3,
            "ignore": 0,
        },
        columns=data._KLINE_COLUMNS,
    )


def _zip_bytes(df: pd.DataFrame, zip_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zip_name.replace(".zip", ".csv"), df.to_csv(index=False, header=False))
    return buf.getvalue()


def _month_frame(year: int, month: int) -> pd.DataFrame:
    first = dt.date(year, month, 1)
    stop = dump_client._first_of_next_month(first)
    frames = []
    day = first
    while day < stop:
        frames.append(_day_frame(day))
        day += dt.timedelta(days=1)
    return pd.concat(frames, ignore_index=True)


def _build_server() -> dict[str, bytes]:
    """The dump files a real server would hold for the [START, END) window."""
    files: dict[str, bytes] = {}
    day = START
    while day < dt.date(2023, 2, 1):
        name = f"{SYMBOL}-1m-{day.isoformat()}.zip"
        files[name] = _zip_bytes(_day_frame(day), name)
        day += dt.timedelta(days=1)
    for year, month in ((2023, 2), (2023, 3)):
        name = f"{SYMBOL}-1m-{year}-{month:02d}.zip"
        files[name] = _zip_bytes(_month_frame(year, month), name)
    day = dt.date(2023, 4, 1)
    while day < END:
        name = f"{SYMBOL}-1m-{day.isoformat()}.zip"
        files[name] = _zip_bytes(_day_frame(day), name)
        day += dt.timedelta(days=1)
    return files


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_get(files, drop=(), bad_checksum=(), no_checksum=False):
    def get(url, timeout=None):
        name = url.rsplit("/", 1)[-1]
        if name.endswith(".CHECKSUM"):
            target = name[: -len(".CHECKSUM")]
            if no_checksum or target not in files or target in drop:
                return _FakeResponse(404)
            digest = hashlib.sha256(files[target]).hexdigest()
            if target in bad_checksum:
                digest = "0" * 64
            return _FakeResponse(200, text=f"{digest}  {target}\n")
        if name not in files or name in drop:
            return _FakeResponse(404)
        return _FakeResponse(200, content=files[name])

    return get


def _patch_requests(monkeypatch, get):
    monkeypatch.setattr(dump_client, "requests", SimpleNamespace(get=get))


def test_fetch_range_month_day_split(monkeypatch):
    files = _build_server()
    _patch_requests(monkeypatch, _fake_get(files))
    df, manifest = dump_client.fetch_range(SYMBOL, START, END, pause=0)

    # Jan 15-31 as dailies, Feb + Mar as monthlies, Apr 1-9 as dailies
    sources = [f["source"] for f in manifest["files"]]
    assert sources == ["daily"] * 17 + ["monthly"] * 2 + ["daily"] * 9
    names = [f["name"] for f in manifest["files"]]
    assert names[0] == f"{SYMBOL}-1m-2023-01-15.zip"
    assert names[17] == f"{SYMBOL}-1m-2023-02.zip"
    assert manifest["missing_days"] == []
    assert manifest["missing_months"] == []
    assert manifest["symbol"] == SYMBOL
    assert manifest["start"] == "2023-01-15"
    assert manifest["end"] == "2023-04-10"
    # every file records rows + the sha256 of the raw zip bytes, verified
    feb = manifest["files"][17]
    assert feb["rows"] == 28 * 1440
    assert feb["sha256"] == hashlib.sha256(files[feb["name"]]).hexdigest()
    assert feb["sha256_verified"] is True
    # the frame spans exactly [START, END) with no duplicates and no gaps
    assert df["open_time"].iloc[0] == pd.Timestamp(START)
    assert df["open_time"].iloc[-1] == pd.Timestamp(END) - pd.Timedelta("1min")
    assert len(df) == N_DAYS * 1440
    assert df["open_time"].is_unique
    assert data.find_gaps(df) == []


def test_fetch_range_records_missing_day(monkeypatch):
    files = _build_server()
    _patch_requests(monkeypatch, _fake_get(files, drop={f"{SYMBOL}-1m-2023-01-20.zip"}))
    df, manifest = dump_client.fetch_range(SYMBOL, START, END, pause=0)
    assert manifest["missing_days"] == ["2023-01-20"]
    assert len(df) == (N_DAYS - 1) * 1440
    gaps = data.find_gaps(df)
    assert len(gaps) == 1
    assert gaps[0][2] == 1440  # the whole missing day, counted in bars


def test_fetch_range_monthly_404_falls_back_to_daily(monkeypatch):
    files = _build_server()
    # the server lacks the February monthly zip but has the daily ones
    day = dt.date(2023, 2, 1)
    while day < dt.date(2023, 3, 1):
        name = f"{SYMBOL}-1m-{day.isoformat()}.zip"
        files[name] = _zip_bytes(_day_frame(day), name)
        day += dt.timedelta(days=1)
    _patch_requests(monkeypatch, _fake_get(files, drop={f"{SYMBOL}-1m-2023-02.zip"}))
    df, manifest = dump_client.fetch_range(SYMBOL, START, END, pause=0)
    assert manifest["missing_months"] == ["2023-02"]
    feb = [f for f in manifest["files"] if f["name"].startswith(f"{SYMBOL}-1m-2023-02-")]
    assert len(feb) == 28
    assert all(f["source"] == "daily" for f in feb)
    assert len(df) == N_DAYS * 1440
    assert data.find_gaps(df) == []


def test_fetch_range_checksum_mismatch_raises(monkeypatch):
    files = _build_server()
    _patch_requests(monkeypatch,
                    _fake_get(files, bad_checksum={f"{SYMBOL}-1m-2023-01-15.zip"}))
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        dump_client.fetch_range(SYMBOL, START, END, pause=0)


def test_fetch_range_unverified_when_checksum_sidecar_missing(monkeypatch):
    files = _build_server()
    _patch_requests(monkeypatch, _fake_get(files, no_checksum=True))
    df, manifest = dump_client.fetch_range(SYMBOL, START, END, pause=0)
    assert len(df) == N_DAYS * 1440
    assert all(f["sha256_verified"] is False for f in manifest["files"])


def _listed_mid_window_server() -> dict[str, bytes]:
    """A symbol listed 2023-03-10: no Jan dailies, no Feb or Mar monthly zip,
    March dailies from the 10th, April dailies."""
    files: dict[str, bytes] = {}
    day = dt.date(2023, 3, 10)
    while day < END:
        name = f"{SYMBOL}-1m-{day.isoformat()}.zip"
        files[name] = _zip_bytes(_day_frame(day), name)
        day += dt.timedelta(days=1)
    return files


def test_fetch_range_from_listing_skips_pre_listing_files(monkeypatch):
    _patch_requests(monkeypatch, _fake_get(_listed_mid_window_server()))
    df, manifest = dump_client.fetch_range(SYMBOL, START, END, pause=0, from_listing=True)
    jan = [(START + dt.timedelta(days=k)).isoformat() for k in range(17)]
    mar = [dt.date(2023, 3, d).isoformat() for d in range(1, 10)]
    # January days, the whole of February (probed once, not 28 dailies), March 1-9
    assert manifest["pre_listing"] == jan + ["2023-02"] + mar
    assert manifest["missing_days"] == []
    assert manifest["missing_months"] == ["2023-03"]  # the listing month had no monthly zip
    assert manifest["listing_start"] == "2023-03-10T00:00:00"
    assert len(df) == (22 + 9) * 1440
    # without the flag the same server is a dataset with holes, reported as such
    _, strict = dump_client.fetch_range(SYMBOL, START, END, pause=0)
    assert len(strict["missing_days"]) == 17 + 28 + 9
    assert strict["pre_listing"] == [] and "listing_start" not in strict


def test_download_run_writes_manifest_and_respects_listing(monkeypatch, tmp_path, capsys):
    from near_bot import download

    _patch_requests(monkeypatch, _fake_get(_listed_mid_window_server()))
    rc = download.run([SYMBOL], START, END, tmp_path, from_listing=True, pause=0)
    assert rc == 0
    mpath = tmp_path / f"{SYMBOL}_1m_20230115_20230410.manifest.json"
    manifest = json.loads(mpath.read_text())
    assert manifest["listing_start"] == "2023-03-10T00:00:00"
    assert manifest["gaps"] == [] and manifest["republished"] == []
    assert manifest["rows"] == (22 + 9) * 1440
    assert manifest["first_bar"] == "2023-03-10 00:00:00"
    assert "listed after the window start" in capsys.readouterr().out
    # strict mode: the 54-day pre-listing stretch is a gap over 1 day -> fail loud
    rc = download.run([SYMBOL], START, END, tmp_path / "strict", pause=0)
    assert rc == 1
    assert "exceed 1 day" in capsys.readouterr().err


def test_download_rerun_reports_republished_file(monkeypatch, tmp_path, capsys):
    from near_bot import download

    files = _build_server()
    _patch_requests(monkeypatch, _fake_get(files))
    assert download.run([SYMBOL], START, END, tmp_path, pause=0) == 0
    # Binance republishes one daily file with corrected prices (and a new checksum)
    name = f"{SYMBOL}-1m-2023-01-16.zip"
    fixed = _day_frame(dt.date(2023, 1, 16))
    fixed["close"] = 1.06
    files[name] = _zip_bytes(fixed, name)
    assert download.run([SYMBOL], START, END, tmp_path, pause=0) == 1
    manifest = json.loads((tmp_path / f"{SYMBOL}_1m_20230115_20230410.manifest.json").read_text())
    assert manifest["republished"] == [name]
    assert "REPUBLISHED" in capsys.readouterr().err


def test_download_gap_fails_until_allow_listed(monkeypatch, tmp_path):
    from near_bot import download

    files = _build_server()
    drop = {f"{SYMBOL}-1m-2023-01-20.zip", f"{SYMBOL}-1m-2023-01-21.zip"}
    _patch_requests(monkeypatch, _fake_get(files, drop=drop))
    assert download.run([SYMBOL], START, END, tmp_path, pause=0) == 1
    mpath = tmp_path / f"{SYMBOL}_1m_20230115_20230410.manifest.json"
    manifest = json.loads(mpath.read_text())
    assert manifest["gaps"] == [{"start": "2023-01-20 00:00:00", "end": "2023-01-21 23:59:00",
                                 "missing_bars": 2880, "allowed": False}]
    # a human allow-lists the known outage, with a reason, in the manifest
    manifest["allowed_gaps"] = [{"start": "2023-01-20", "end": "2023-01-22",
                                 "reason": "exchange maintenance (test)"}]
    mpath.write_text(json.dumps(manifest))
    assert download.run([SYMBOL], START, END, tmp_path, pause=0) == 0
    manifest = json.loads(mpath.read_text())
    assert manifest["gaps"][0]["allowed"] is True
    assert manifest["allowed_gaps"][0]["reason"] == "exchange maintenance (test)"


def test_fetch_range_rejects_empty_window():
    with pytest.raises(ValueError, match="Empty window"):
        dump_client.fetch_range(SYMBOL, START, START)


def test_config_load_data_section(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """symbol: SOLUSDT
data:
  symbols: [NEARUSDT, BTCUSDT]
  start: "2023-01-01"
  end: "2026-01-01"
  dir: datasets
"""
    )
    cfg = Config.load(cfg_path)
    assert cfg.symbol == "SOLUSDT"
    assert cfg.data.symbols == ["NEARUSDT", "BTCUSDT"]
    assert cfg.data.start == "2023-01-01"
    assert cfg.data.end == "2026-01-01"
    assert cfg.data.dir == "datasets"
    d = cfg.to_dict()
    assert d["data"]["symbols"] == ["NEARUSDT", "BTCUSDT"]
    assert "api_key" not in d
    assert "api_secret" not in d


def test_config_data_defaults():
    cfg = Config()
    assert cfg.data.symbols == ["NEARUSDT", "ETHUSDT", "SOLUSDT", "BTCUSDT"]
    assert cfg.data.start == "2023-01-01"
    assert cfg.data.end == ""
    assert cfg.data.dir == "data"

"""Pre-registered plateau hunt (experiments/plateau_hunt.py).

The selection rule is what the whole funnel stands on, so it is pinned on
constructed score grids where the right answer is known: a broad plateau
must beat a taller isolated spike, ineligible cells can never be selected,
the stability rule must veto a selection that fails in either half, and the
search span must never admit a holdout bar.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from conftest import make_1m_klines

from near_bot import data
from near_bot.config import Config

_PATH = Path(__file__).resolve().parent.parent / "experiments" / "plateau_hunt.py"
_spec = importlib.util.spec_from_file_location("plateau_hunt", _PATH)
ph = importlib.util.module_from_spec(_spec)
sys.modules["plateau_hunt"] = ph
_spec.loader.exec_module(ph)

SHAPE = (5, 5, 5)


def _idx(*pos):
    return int(np.ravel_multi_index(pos, SHAPE))


def _plateau_and_spike():
    s = np.zeros(SHAPE)
    s[0, 0, 0] = 2.0              # the luckiest single cell
    s[2:5, 2:5, 2:5] = 0.6        # a broad plateau
    return s.ravel()


def test_plateau_beats_a_taller_isolated_spike():
    scores = _plateau_and_spike()
    assert int(np.argmax(scores)) == _idx(0, 0, 0)  # raw best = the spike
    neigh = ph.neighbourhood_scores(scores, SHAPE)
    assert neigh[_idx(0, 0, 0)] == pytest.approx(2.0 / 8)  # corner: 8 cells
    center = ph.select_center(neigh, np.ones(125, dtype=bool))
    assert center == _idx(3, 3, 3)  # first in grid order among the 0.6 ties


def test_ineligible_cells_are_never_selected():
    neigh = ph.neighbourhood_scores(_plateau_and_spike(), SHAPE)
    eligible = np.ones(125, dtype=bool)
    eligible[_idx(3, 3, 3)] = False
    assert ph.select_center(neigh, eligible) == _idx(3, 3, 4)
    assert ph.select_center(neigh, np.zeros(125, dtype=bool)) is None


def test_boundary_knobs_flag_edge_selections():
    assert ph.boundary_knobs(_idx(0, 2, 4), ph.GRID) == ["theta_in", "er_min"]
    assert ph.boundary_knobs(_idx(2, 2, 2), ph.GRID) == []


def test_stability_rule_vetoes_failure_in_either_half():
    center = _idx(2, 2, 2)
    good = {"scores": np.full(125, 0.1), "top_fraction": 0.0}
    assert not ph.stability(center, [good, good])["contradicted"]
    negative = {"scores": np.full(125, -0.1), "top_fraction": 0.0}
    st = ph.stability(center, [good, negative])
    assert st["contradicted"] and "half 2" in st["reasons"][0]
    ranked_low = {"scores": np.full(125, 0.1), "top_fraction": 0.30}
    assert ph.stability(center, [ranked_low, good])["contradicted"]


def test_top_fraction_and_rank_correlation():
    neigh = np.arange(10, dtype=float)
    eligible = np.ones(10, dtype=bool)
    assert ph.top_fraction(neigh, eligible, 9) == 0.0
    assert ph.top_fraction(neigh, eligible, 0) == pytest.approx(0.9)
    eligible[5] = False
    assert ph.top_fraction(neigh, eligible, 5) == 1.0  # ineligible = outside
    assert ph.rank_correlation([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert ph.rank_correlation([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_search_slice_never_admits_a_holdout_bar():
    ts = pd.date_range("2024-12-30", "2025-01-03", freq="1h")
    bars = pd.DataFrame({"timestamp": ts, "close": 1.0})
    out = ph.search_slice(bars)
    assert out["timestamp"].max() < pd.Timestamp(ph.HOLDOUT[0])
    assert out["timestamp"].min() == ts[0]


def test_parse_center_accepts_only_grid_cells():
    assert ph._parse_center(["theta_in=0.25", "trail_atr=3.0", "er_min=0.2"]) == {
        "theta_in": 0.25, "trail_atr": 3.0, "er_min": 0.2}
    with pytest.raises(SystemExit):
        ph._parse_center(["theta_in=0.27", "trail_atr=3.0", "er_min=0.2"])
    with pytest.raises(SystemExit):
        ph._parse_center(["theta_in=0.25"])


def _synthetic():
    cfg = Config()
    cfg.aggregate_minutes = 15
    bars = {s: data.aggregate(make_1m_klines(24000, seed=seed), 15, drop_incomplete=True)
            for s, seed in zip(ph.SYMBOLS, (7, 8, 9), strict=True)}
    return cfg, bars


def test_hunt_end_to_end_on_synthetic_data():
    cfg, bars = _synthetic()
    grid = {"theta_in": [0.15, 0.25, 0.35], "trail_atr": [2.5, 3.5], "er_min": [0.15, 0.25]}
    halves = (("2026-01-01", "2026-01-09"), ("2026-01-09", "2026-01-20"))
    res = ph.hunt(bars, cfg, grid=grid, min_trades=1, halves=halves, min_trades_half=0,
                  n_boot=50, block=24)
    assert res["center"] is not None
    assert len(res["halves"]) == 2 and "contradicted" in res["stability"]
    assert res["center_block"]["dsr_trials"] == ph.stats.TRIALS_TRIED
    text = ph.render(res, cfg)
    for needle in ("## (a) Plateau map", "## Selected plateau center",
                   "## (b) Stability check", "## Verdict", ph.HOLDOUT_STATUS):
        assert needle in text
    table = ph.grid_table(res, cfg)
    assert len(table) == 12 and table["selected"].sum() == 1


def test_holdout_runs_one_fixed_config():
    cfg, bars = _synthetic()
    out = ph.holdout(bars, cfg, {"theta_in": 0.25, "trail_atr": 3.0, "er_min": 0.2},
                     n_boot=50, block=24)
    assert set(out["per_symbol"]) == set(ph.SYMBOLS)
    assert "verdict" in out["block"]
    assert "sealed-holdout" in ph.render_holdout(out, {"theta_in": 0.25}, cfg)

#!/usr/bin/env python3
"""Plateau-robustness sweep: perturb one default parameter at a time and
report walk-forward OOS performance. A good default sits on a plateau.

    python experiments/sweep.py --csv data/klines_1m.csv
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from near_bot import data  # noqa: E402
from near_bot import walkforward as wf  # noqa: E402
from near_bot.config import Config  # noqa: E402

PERTURB = {
    "trend_window": [100, 150, 250, 300],
    "mom_slow": [72, 96, 168],
    "mom_fast": [24, 48],
    "theta_out": [-0.1, 0.1],
    "w_ofi": [0.0, 0.35],
    "target_vol": [0.25, 0.55, 0.70],
    "theta_in": [0.15, 0.35],
    "trail_atr": [2.5, 3.5],
    "er_min": [0.15, 0.25],
}


def run_one(bars, cfg, **over):
    cfg2 = cfg
    if over:
        import copy

        cfg2 = copy.copy(cfg)
        cfg2.strategy = replace(cfg.strategy, **over)
    res = wf.run(bars, cfg2)
    m = res.oos_metrics
    return m.get("total_return", 0.0), m.get("sharpe", 0.0), m.get("n_trades", 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/klines_1m.csv")
    args = ap.parse_args(argv)

    cfg = Config()
    bars = data.aggregate(data.load_csv(args.csv), cfg.aggregate_minutes,
                          drop_incomplete=True)
    ret, sh, n = run_one(bars, cfg)
    print(f"{'default':<28} {ret:+.1%}  sharpe {sh:+.2f}  n={n}")
    for knob, vals in PERTURB.items():
        for v in vals:
            ret, sh, n = run_one(bars, cfg, **{knob: v})
            print(f"{knob}={v!s:<22} {ret:+.1%}  sharpe {sh:+.2f}  n={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

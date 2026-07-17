#!/usr/bin/env python3
"""Generate the shared fixture data + Python reference outputs for the
JS/Python engine parity check.

Usage:
    python experiments/make_fixtures.py
    node experiments/parity_check.js

Writes /tmp/fixture_1m.json (raw 1-minute klines) and /tmp/py_reference.json
(Python engine results for several parameter/cost/aggregation cases). The
parity check reruns those cases through docs/engine.js and demands identical
trades and matching metrics.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import make_1m_klines  # noqa: E402

from near_bot import backtest as bt  # noqa: E402
from near_bot import data, indicators, score as score_mod  # noqa: E402
from near_bot.config import Config  # noqa: E402

CASES = [
    {"name": "defaults_60m", "agg": 60, "params": {}, "costs": {}},
    {"name": "wide_band_60m", "agg": 60,
     "params": {"theta_in": 0.35, "theta_out": 0.10, "trail_atr": 2.5, "er_min": 0.25},
     "costs": {}},
    {"name": "fast_15m", "agg": 15,
     "params": {"mom_fast": 24, "mom_slow": 72, "trend_window": 100,
                "target_vol": 0.25, "max_hold_bars": 96},
     "costs": {"taker_fee": 0.00075, "slippage": 0.0005}},
    {"name": "zero_costs", "agg": 60, "params": {"min_weight": 0.0},
     "costs": {"taker_fee": 0.0, "slippage": 0.0}},
]

KEEP_METRICS = ["n_trades", "total_return", "sharpe", "sortino", "max_drawdown",
                "exposure", "win_rate", "expectancy_weighted", "cost_drag",
                "profit_factor", "turnover", "buy_hold"]


def main() -> None:
    df = make_1m_klines(24000, seed=7)
    rows = [[int(t.value // 10**6), float(o), float(h), float(lo), float(c),
             float(v), float(tb)]
            for t, o, h, lo, c, v, tb in zip(
                df.open_time, df.open, df.high, df.low, df.close,
                df.volume, df.taker_buy_base, strict=True)]
    json.dump(rows, open("/tmp/fixture_1m.json", "w"))

    out = []
    for case in CASES:
        cfg = Config()
        cfg.aggregate_minutes = case["agg"]
        cfg.strategy = replace(cfg.strategy, **case["params"])
        for k, v in case["costs"].items():
            setattr(cfg.costs, k, v)
        bars = data.aggregate(df, cfg.aggregate_minutes, drop_incomplete=True)
        frame = indicators.add_all(bars, cfg.strategy)
        sc = score_mod.compute(frame, cfg.strategy)
        res = bt.run(frame, cfg, score=sc)
        out.append({
            "name": case["name"], "agg": case["agg"],
            "params": case["params"], "costs": case["costs"],
            "n_bars": len(bars),
            "trades": [[int(t.entry_time.value // 10**6), int(t.exit_time.value // 10**6),
                        t.entry_price, t.exit_price, t.weight, t.reason_out,
                        t.gross_pct, t.net_pct, t.weighted_net, t.bars_held]
                       for t in res.trades],
            "metrics": {k: (None if res.metrics.get(k) == float("inf") else res.metrics.get(k))
                        for k in KEEP_METRICS},
            "equity_final": float(res.equity.iloc[-1]),
        })
        print(f"  {case['name']}: {len(res.trades)} trades, "
              f"equity {res.equity.iloc[-1]:.6f}")
    json.dump(out, open("/tmp/py_reference.json", "w"))
    print("Wrote /tmp/fixture_1m.json and /tmp/py_reference.json")


if __name__ == "__main__":
    main()

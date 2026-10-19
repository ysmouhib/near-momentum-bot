#!/usr/bin/env python3
"""Post-hoc context for reports/plateau_hunt.md: the selected cell vs buy-and-hold.

Written *after* the pre-registered plateau hunt (4ecfe2c) had run, and never
used for selection or for any pass/fail rule. It answers the question the
hunt's result raises — the search converged on the loosest corner of the
grid, i.e. on more time in the market — by putting the selected cell next to
simply holding the same coins over the same 2021-2024 bars, with the same
per-bar accounting (Sharpe on 60m bar returns, flat bars included). Prints a
markdown section; the holdout is never read (plateau_hunt.search_slice).

    python experiments/plateau_context.py
"""

from __future__ import annotations

import copy
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "experiments"))

import numpy as np
import pandas as pd
import plateau_hunt as ph

from near_bot import backtest as bt
from near_bot import indicators, stats
from near_bot import score as score_mod
from near_bot.config import Config


def selected_cell(csv_path: Path) -> dict:
    """The cell the pre-registered hunt selected, read from its own output."""
    row = pd.read_csv(csv_path).query("selected").iloc[0]
    return {k: float(row[k]) for k in ph.GRID}


def compare(bars_by_symbol: dict[str, pd.DataFrame], cfg, combo: dict) -> list[dict]:
    """Per-symbol and pooled rows: buy-and-hold vs the selected cell."""
    ann = np.sqrt(indicators.bars_per_year(cfg.aggregate_minutes))
    params = replace(cfg.strategy, **combo)
    sub = copy.copy(cfg)
    sub.strategy = params
    rows, bh_parts, cell_parts = [], {}, {}
    for symbol, bars in bars_by_symbol.items():
        frame = indicators.add_all(bars, params)
        res = bt.run(frame, sub, score_mod.compute(frame, params))
        ts = pd.DatetimeIndex(bars["timestamp"])
        bh = bars["close"].pct_change().fillna(0.0).to_numpy()
        cell = res.bar_returns.to_numpy()
        bh_parts[symbol] = pd.Series(bh, index=ts)
        cell_parts[symbol] = pd.Series(cell, index=ts)
        rows.append({"name": symbol, "bh": bh, "cell": cell,
                     "exposure": float((res.position > 0).mean())})
    pooled_bh = pd.DataFrame(bh_parts).mean(axis=1).to_numpy()
    pooled_cell = pd.DataFrame(cell_parts).mean(axis=1).to_numpy()
    rows.append({"name": "Pooled (equal weight)", "bh": pooled_bh, "cell": pooled_cell,
                 "exposure": float(np.mean([r["exposure"] for r in rows]))})
    for r in rows:
        r["bh_sharpe"] = float(r["bh"].mean() / r["bh"].std() * ann)
        r["cell_sharpe"] = float(r["cell"].mean() / r["cell"].std() * ann)
        r["bh_total"] = stats.total_return_stat(r["bh"])
        r["cell_total"] = stats.total_return_stat(r["cell"])
        r["bh_mdd"] = stats.max_drawdown_stat(r["bh"])
        r["cell_mdd"] = stats.max_drawdown_stat(r["cell"])
        r["corr"] = float(np.corrcoef(r["bh"], r["cell"])[0, 1])
    return rows


def render(rows: list[dict], combo: dict) -> str:
    cell = ", ".join(f"{k}={v:g}" for k, v in combo.items())
    lines = [
        "## Post-hoc context: selected cell vs buy-and-hold, 2021-2024", "",
        ("> **Post-hoc, descriptive only.** Computed after the pre-registered run "
         "(4ecfe2c) by `experiments/plateau_context.py`; not part of the "
         "pre-registration and not used for selection or any pass/fail rule."), "",
        (f"Same bars, same per-bar accounting. Selected cell: `{cell}`. Buy-and-hold "
         "= the coin's own close-to-close returns, fully invested; the pooled row "
         "rebalances to equal weight every bar."), "",
        ("| | Sharpe: buy-and-hold | Sharpe: selected cell | total return: buy-and-hold "
         "| total return: selected cell | max drawdown: buy-and-hold | max drawdown: "
         "selected cell | cell exposure | correlation |"),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['name']} | {r['bh_sharpe']:+.2f} | {r['cell_sharpe']:+.2f} | "
                     f"{r['bh_total']:+.0%} | {r['cell_total']:+.0%} | {r['bh_mdd']:.0%} | "
                     f"{r['cell_mdd']:.0%} | {r['exposure']:.0%} | {r['corr']:+.2f} |")
    lines += ["", ("Exposure = share of bars with an open position; correlation = between "
                   "the per-bar returns of the cell and of buy-and-hold."), ""]
    return "\n".join(lines)


def main() -> int:
    cfg = Config.load(ROOT / "config.yaml")
    combo = selected_cell(ROOT / "reports" / "plateau_hunt.csv")
    full = ph.load_bars(ROOT / cfg.data.dir, cfg)
    bars = {s: ph.search_slice(b) for s, b in full.items()}
    print(render(compare(bars, cfg, combo), combo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

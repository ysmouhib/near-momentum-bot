"""The validation battery: multi-symbol walk-forward with honest statistics.

Why a battery, not a command
----------------------------
v2's own README already concedes "not distinguishable from noise at this
sample size" on 91 days. Hardening into Trend Rider changes the data
(multi-year fixed windows, several symbols) and the machinery (risk overlay),
but not the epistemics: a number only counts if its uncertainty band excludes
zero after charging for everything we tried. This module assembles, for each
symbol and for the pooled cross-symbol portfolio:

* walk-forward OOS returns (Sharpe-selected, top-k ensemble — unchanged),
* block-bootstrap CIs on Sharpe / total return / per-decision expectancy,
* the deflated Sharpe ratio against the trials ledger (stats.TRIALS_TRIED),
* N_eff: independent decisions after clustering overlapping trades,
* a mandatory plain-words verdict when a result is statistically thin
  (docs/DESIGN.md §7.6 — the noise clause is the deliverable when true).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import stats
from . import walkforward as wf


def evaluate_oos(res: wf.WalkForwardResult, bar_minutes: int, top_k: int,
                 n_boot: int, block: int, seed: int = 7,
                 n_grid_cells: int = 12) -> dict:
    """Bootstrap CIs, deflated Sharpe and N_eff for one stitched OOS path."""
    oos = res.oos_returns.to_numpy(dtype=float)
    out: dict = {"n_bars": len(oos), "n_trades": res.oos_metrics.get("n_trades", 0)}
    if len(oos) < 10:
        out["verdict"] = "insufficient OOS data"
        return out

    sr_pt, sr_lo, sr_hi = stats.bootstrap_ci(
        oos, stats.sharpe_stat(bar_minutes), n_boot, block, seed)
    tr_pt, tr_lo, tr_hi = stats.bootstrap_ci(
        oos, stats.total_return_stat, n_boot, block, seed + 1)

    dc = stats.decision_clusters(res.oos_trades)
    pnls = dc.cluster_pnls(scale=1.0 / top_k)
    if len(pnls) >= 3:
        e_pt, e_lo, e_hi = stats.bootstrap_ci(
            pnls, stats.mean_stat, n_boot, max(1, min(5, len(pnls))), seed + 2)
    else:
        e_pt, e_lo, e_hi = (float(pnls.mean()) if len(pnls) else 0.0,
                            float("nan"), float("nan"))

    sd = oos.std()
    sr_bar = float(oos.mean() / sd) if sd > 0 else 0.0
    trial_sharpes = [s for w in res.windows for s in w.in_sample_sharpe]
    traded_windows = sum(1 for w in res.windows if w.combos is not None)
    dsr = stats.deflated_sharpe(
        sr_bar, len(oos), trial_sharpes,
        skew=stats.skewness(oos), kurt=stats.kurtosis(oos),
        n_trials=max(stats.TRIALS_TRIED, traded_windows * n_grid_cells))

    out.update({
        "sharpe": (sr_pt, sr_lo, sr_hi),
        "total_return": (tr_pt, tr_lo, tr_hi),
        "expectancy_per_decision": (e_pt, e_lo, e_hi),
        "n_eff": dc.n_eff,
        "dsr": dsr["dsr"], "dsr_benchmark": dsr["sr_star"],
        "dsr_trials": dsr["n_trials"],
        "windows": len(res.windows), "windows_traded": traded_windows,
    })
    if sr_lo <= 0.0 or not np.isfinite(sr_lo):
        out["verdict"] = ("NOT statistically distinguishable from noise at this "
                          "sample size (Sharpe CI includes zero)")
    elif dsr["dsr"] < 0.95:
        out["verdict"] = (f"Sharpe CI excludes zero but DSR {dsr['dsr']:.2f} < 0.95: "
                          "not established after charging for configurations tried")
    else:
        out["verdict"] = ("Sharpe CI excludes zero and survives deflation — "
                          "provisionally established, keep measuring")
    return out


def pooled_returns(per_symbol: dict[str, tuple], bars_by_symbol: dict) -> pd.Series:
    """Cross-symbol per-bar mean of OOS returns, aligned by timestamp.

    Windows are calendar-aligned across symbols (same fixed window), so the
    pooled series is the equity path of running the bot on all symbols at
    once with capital split equally across the active ones.
    """
    parts = {}
    for symbol, res in per_symbol.items():
        if not len(res.oos_returns):
            continue
        ts = bars_by_symbol[symbol]["timestamp"].reset_index(drop=True)
        idx = list(res.oos_returns.index)
        parts[symbol] = pd.Series(res.oos_returns.to_numpy(),
                                  index=pd.DatetimeIndex(ts.iloc[idx]))
    if not parts:
        return pd.Series(dtype=float)
    return pd.DataFrame(parts).mean(axis=1, skipna=True).sort_index()


def format_block(name: str, block: dict) -> list[str]:
    lines = [f"### {name}", ""]
    if "sharpe" not in block:
        lines.append(f"_{block.get('verdict', 'no data')}_")
        return lines
    spt, slo, shi = block["sharpe"]
    tpt, tlo, thi = block["total_return"]
    ept, elo, ehi = block["expectancy_per_decision"]
    lines += [
        f"- OOS bars {block['n_bars']}, trades {block['n_trades']}, "
        f"**independent decisions N_eff = {block['n_eff']}**",
        f"- Sharpe (ann.): {spt:+.2f}  95% CI [{slo:+.2f}, {shi:+.2f}]",
        f"- Total return: {tpt:+.2%}  95% CI [{tlo:+.2%}, {thi:+.2%}]",
        f"- Expectancy / decision: {ept:+.3%}  95% CI [{elo:+.3%}, {ehi:+.3%}]",
        f"- Deflated Sharpe: {block['dsr']:.3f} (benchmark SR* "
        f"{block['dsr_benchmark']:+.2f}, N trials {block['dsr_trials']})",
        f"- Windows: {block['windows_traded']}/{block['windows']} traded",
        f"- **Verdict: {block['verdict']}**",
        "",
    ]
    return lines


def render_report(per_symbol_blocks: dict[str, dict], pooled_block: dict | None,
                  header: str) -> str:
    lines = ["# Validation report", "", header, ""]
    for name, block in per_symbol_blocks.items():
        lines += format_block(name, block)
    if pooled_block:
        lines += format_block("POOLED (equal-weight across symbols, "
                              "calendar-aligned windows)", pooled_block)
    lines += [
        "---",
        "Noise clause (DESIGN.md §7.6): any block whose CI includes zero is not "
        "statistically distinguishable from noise at this sample size, and is "
        "reported as such. DSR charges for every configuration tried "
        "(stats.TRIALS_TRIED).",
        "",
        "Not financial advice.",
    ]
    return "\n".join(lines)

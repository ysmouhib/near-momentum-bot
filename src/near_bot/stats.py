"""Statistical honesty: bootstrap CIs, deflated Sharpe, decision counting.

Why this module exists
----------------------
A backtest number without an uncertainty band is a marketing claim. v2's own
headline ("+11% OOS on 91 days") rests on ~13 *independent* decisions — the
ensemble legs and overlapping trades are correlated, so counting trades
overstates the evidence. This module provides the three instruments the
validation protocol (docs/DESIGN.md §7) runs on every result:

* **Block bootstrap CIs** — per-bar returns are serially correlated, so
  resampling single bars would invent independence that is not there.
  Resampling contiguous *blocks* (default 168 bars ≈ 1 week) preserves the
  local autocorrelation structure. Same reasoning for human regime calls:
  resample streaks, not calls.
* **Deflated Sharpe ratio** (Bailey & López de Prado, 2014) — every
  configuration tried during the project inflates the expected maximum
  Sharpe under the null. DSR charges for them via the ``TRIALS_TRIED``
  ledger below. It is deliberately conservative: when in doubt, overcount.
* **Decision clustering** — trades whose holding intervals overlap (across
  top-k ensemble legs, across symbols) are one decision, counted once.

Everything here is seeded and deterministic: a CI that changes between runs
would be its own kind of noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np

_NORM = NormalDist()
_EULER = 0.5772156649015329

# Trials ledger for the deflated Sharpe ratio — every configuration evaluated
# during the project counts as a lottery ticket, whether or not it was kept:
#   v1 checklist grids (documented in v1 research) .......... ~40
#   v2 DEFAULT_GRID (3×2×2) .................................  12
#   v2 plateau sweep, one knob at a time (~24 points) .......  24
#   window-size / timeframe variants tried ..................  ~8
#   overlay threshold choices considered (DESIGN.md §9) ..... ~10
#   pre-registered plateau hunt 2026-10, 5x5x5 grid over
#     theta_in x trail_atr x er_min (experiments/plateau_hunt.py),
#     charged before any cell was scored .................... 125
# Bump this whenever a new configuration is evaluated. Overcounting lowers
# DSR; that is the intended direction of error.
TRIALS_TRIED = 219


# ------------------------------------------------------------------ moments

def skewness(x: np.ndarray) -> float:
    """Fisher-Pearson skewness; 0 when the sample is too small or degenerate."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3:
        return 0.0
    m = x.mean()
    s = x.std()
    if s <= 0:
        return 0.0
    return float(((x - m) ** 3).mean() / s**3)


def kurtosis(x: np.ndarray) -> float:
    """Non-excess kurtosis (3.0 for a normal); 3.0 on degenerate input."""
    x = np.asarray(x, dtype=float)
    if len(x) < 4 or x.std() <= 0:
        return 3.0
    m = x.mean()
    return float(((x - m) ** 4).mean() / x.std() ** 4)


# ----------------------------------------------------------------- bootstrap

def _block_resample(x: np.ndarray, block: int, rng: np.random.Generator) -> np.ndarray:
    """Circular block bootstrap: wrap-around blocks preserve short-range
    autocorrelation while staying unbiased for the sample mean.

    Vectorised: one draw per block start, the last block truncated to fit.
    The block starts come from a single ``rng.integers(..., size=k)`` call,
    which consumes the generator exactly like k scalar draws, so seeded CIs
    are unchanged by the vectorisation.
    """
    n = len(x)
    block = max(1, min(block, n))
    starts = rng.integers(0, n, size=-(-n // block))
    idx = (starts[:, None] + np.arange(block)).ravel()[:n] % n
    return x[idx]


def bootstrap_ci(x: np.ndarray, stat_fn, n_boot: int = 10000, block: int = 168,
                 seed: int = 7, alpha: float = 0.05) -> tuple[float, float, float]:
    """Percentile bootstrap CI for ``stat_fn`` over a 1-D sample.

    Returns ``(point_estimate, lo, hi)``. The resampling unit is the block,
    never the single observation.
    """
    x = np.asarray(x, dtype=float)
    if len(x) == 0:
        return (float("nan"), float("nan"), float("nan"))
    point = float(stat_fn(x))
    rng = np.random.default_rng(seed)
    boots = np.empty(n_boot)
    for b in range(n_boot):
        boots[b] = stat_fn(_block_resample(x, block, rng))
    lo, hi = np.percentile(boots, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return (point, float(lo), float(hi))


def sharpe_stat(bar_minutes: int):
    """Annualised-Sharpe statistic closure for ``bootstrap_ci``."""
    bpy = 365.0 * 24.0 * 60.0 / bar_minutes

    def _f(x: np.ndarray) -> float:
        sd = x.std()
        return float(x.mean() / sd * np.sqrt(bpy)) if sd > 0 else 0.0

    return _f


def total_return_stat(x: np.ndarray) -> float:
    return float(np.prod(1.0 + x) - 1.0)


def ann_return_stat(bar_minutes: int):
    """Geometric annualised-return closure (log space: no overflow on long runs)."""
    bpy = 365.0 * 24.0 * 60.0 / bar_minutes

    def _f(x: np.ndarray) -> float:
        growth = np.log1p(x).sum()
        return float(np.expm1(growth * bpy / len(x))) if len(x) else 0.0

    return _f


def max_drawdown_stat(x: np.ndarray) -> float:
    """Deepest peak-to-trough loss of the compounded path (<= 0)."""
    equity = np.cumprod(1.0 + x)
    return float((equity / np.maximum.accumulate(equity) - 1.0).min()) if len(x) else 0.0


def mean_stat(x: np.ndarray) -> float:
    return float(x.mean())


def wilson_ci(k: float, n: float, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a proportion ``k / n``.

    ``k`` and ``n`` may be fractional *effective* counts (e.g. n / block for
    serially dependent calls). Unlike a percentile bootstrap it never
    collapses to zero width — five correct calls out of five still leave a
    lower bound far below 100% — which is the honest answer for a short
    record.
    """
    if n <= 0:
        return (0.0, 1.0)
    z = _NORM.inv_cdf(1 - alpha / 2)
    phat = k / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


# ------------------------------------------------------------ deflated sharpe

def expected_max_sharpe(n_trials: int, var_sharpe: float) -> float:
    """E[max SR] over N independent trials under the null (SR = 0).

    Bailey & López de Prado (2014), eq. (5): the Gaussian extreme-value
    approximation ``√V · [(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))]``.
    """
    if n_trials < 2 or var_sharpe <= 0:
        return 0.0
    n = float(n_trials)
    return math.sqrt(var_sharpe) * (
        (1 - _EULER) * _NORM.inv_cdf(1 - 1 / n)
        + _EULER * _NORM.inv_cdf(1 - 1 / (n * math.e))
    )


def deflated_sharpe(sr: float, n_obs: int, trial_sharpes, skew: float | None = None,
                    kurt: float | None = None, n_trials: int | None = None) -> dict:
    """Probabilistic Sharpe ratio against the selection-bias benchmark.

    ``sr`` is the observed per-observation (e.g. per-bar) Sharpe of the chosen
    configuration; ``trial_sharpes`` are the Sharpe estimates of evaluated
    trials in the same units (their variance is √V in the benchmark).
    ``n_trials`` overrides the count — pass the trials ledger
    (``TRIALS_TRIED``), which charges for configurations tried but never
    written down anywhere else. Returns the benchmark SR*, the DSR
    probability, and the inputs for the report.
    """
    trial_sharpes = np.asarray(list(trial_sharpes), dtype=float)
    trial_sharpes = trial_sharpes[np.isfinite(trial_sharpes)]
    n_trials = max(n_trials or 0, len(trial_sharpes), 2)
    var_sh = float(trial_sharpes.var(ddof=1)) if len(trial_sharpes) > 1 else 0.0
    sr_star = expected_max_sharpe(n_trials, var_sh)
    if n_obs < 2:
        return {"dsr": 0.0, "sr_star": sr_star, "n_trials": n_trials, "var_sharpe": var_sh}
    # skew/kurt of the return series are supplied by the caller; default normal.
    g3 = 0.0 if skew is None else skew
    g4 = 3.0 if kurt is None else kurt
    denom = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4.0 * sr**2))
    dsr = _NORM.cdf((sr - sr_star) * math.sqrt(n_obs - 1) / denom)
    return {"dsr": float(dsr), "sr_star": float(sr_star),
            "n_trials": n_trials, "var_sharpe": var_sh}


# -------------------------------------------------------- decision clustering

@dataclass
class DecisionClusters:
    """Trades grouped into independent decisions by holding-interval overlap."""

    clusters: list[list] = field(default_factory=list)

    @property
    def n_eff(self) -> int:
        return len(self.clusters)

    def cluster_pnls(self, scale: float = 1.0) -> np.ndarray:
        """One portfolio P&L per decision (sum of member weighted_net)."""
        return np.array([
            sum(t.weighted_net for t in cluster) * scale for cluster in self.clusters
        ])


def decision_clusters(trades) -> DecisionClusters:
    """Group trades into decisions: overlapping [entry, exit] ⇒ one decision.

    The top-k ensemble often holds 2–3 legs over the same hours, and
    multi-symbol runs enter on the same market-wide moves; counting those as
    independent evidence is the classic way backtests lie with sample sizes.
    """
    ordered = sorted(trades, key=lambda t: (t.entry_time, t.exit_time))
    clusters: list[list] = []
    for t in ordered:
        if clusters and t.entry_time <= max(u.exit_time for u in clusters[-1]):
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return DecisionClusters(clusters)

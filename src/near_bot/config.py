"""Typed configuration.

Secrets (API keys) come from environment variables only — they are never
written to the YAML file that lives in the repo. Everything else comes from
``config.yaml`` so runs are reproducible and reviewable in version control.

The v2 defaults were chosen on 91 days of real NEARUSDT 1m data by
walk-forward validation, then checked for *plateau robustness* (every knob
perturbed one at a time; see docs/STRATEGY.md §7). They are a starting point,
not holy writ — revalidate on fresh data before trading them.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a declared dependency
    yaml = None


@dataclass
class ScoreParams:
    """Parameters of the score-ensemble trend strategy.

    All lookbacks are in aggregated bars (default 60m). Momentum windows of
    36/120 bars ≈ 1.5/5 days; trend filter 200 bars ≈ 8 days.
    """

    # Momentum (vol-normalised TSMOM)
    mom_fast: int = 36
    mom_slow: int = 120
    vol_window: int = 48       # realised-vol estimation window

    # Breakout (Donchian position in [-1, 1])
    bo_window: int = 48

    # Order flow (smoothed taker-imbalance z-score)
    ofi_window: int = 24

    # Blend weights (should sum to ~1; the score is clipped to [-1, 1] anyway)
    w_mom: float = 0.50
    w_bo: float = 0.30
    w_ofi: float = 0.20

    # Regime gates
    er_window: int = 48        # Kaufman efficiency-ratio window
    er_min: float = 0.20       # below this the market is chop: no trades
    trend_window: int = 200    # long SMA filter: only long above it

    # Hysteresis band (the cost-aware no-trade region)
    theta_in: float = 0.25     # enter when score crosses above
    theta_out: float = 0.0     # exit when score falls below

    # Exits
    atr_period: int = 14
    trail_atr: float = 3.0     # chandelier: peak close minus trail_atr * ATR
    max_hold_bars: int = 168   # time stop (168h = 1 week at 60m)

    # Volatility-targeted sizing
    target_vol: float = 0.40   # annualised vol budget for the position
    min_weight: float = 0.05   # skip entries sizing below this (dust)


@dataclass
class RiskParams:
    """Live-trading risk limits (the backtester sizes by vol target instead)."""

    max_notional_frac: float = 0.95  # never deploy more than this of free USDT
    cooldown_bars: int = 0           # bars to wait after an exit (live only)


@dataclass
class CostParams:
    """Trading costs. Defaults are Binance spot taker without discounts.

    0.10% per side + 0.02% slippage is ~0.24% round trip — at 4m bars that is
    an unbeatable headwind (v1 lost 82% OOS proving it); at 60m it is merely
    expensive. If you pay fees in BNB, set taker_fee: 0.00075.
    """

    taker_fee: float = 0.001
    slippage: float = 0.0002


@dataclass
class DataParams:
    """Fixed download/validation windows.

    A rolling "last N days" window makes every run a *different* dataset, so
    results drift even when nothing changed. A fixed [start, end) window
    means the same command reproduces the same bytes, the same bars, and the
    same numbers — the property every validation claim stands on.
    """

    symbols: list[str] = field(
        default_factory=lambda: ["NEARUSDT", "ETHUSDT", "SOLUSDT", "BTCUSDT"])
    start: str = "2023-01-01"
    end: str = ""            # empty = latest complete UTC day
    dir: str = "data"


@dataclass
class OverlayParams:
    """The risk overlay: human permission + mechanical brakes (DESIGN.md §4).

    These thresholds are *risk policy chosen a priori*, not fitted parameters
    — they are never walk-forward optimised, and every value tried still goes
    into the deflated-Sharpe trials ledger (stats.TRIALS_TRIED). The defaults
    encode the kill-switch ladder: −2% on the UTC day halts new entries, −5%
    from the equity high-water mark halves size (cleared at −3% so the tier
    cannot flap), −10% flattens and demands a human re-arm.
    """

    enabled: bool = True          # paper/live force this on; backtests default off
    per_trade_risk_frac: float = 0.006   # R per trade, as a fraction of equity
    max_exposure_frac: float = 0.40      # quarter-Kelly cap on invested notional
    hard_max_weight: float = 1.00        # structural (spot, no leverage)
    daily_loss_halt_frac: float = -0.02  # vs UTC day-start equity
    hwm_half_size_frac: float = -0.05    # drawdown from HWM → half size
    hwm_half_clear_frac: float = -0.03   # recovery hysteresis clearing that tier
    hwm_full_stop_frac: float = -0.10    # flatten + require human re-arm
    transition_size_mult: float = 0.5    # size multiplier right after a human arm
    cooldown_trades_after_arm: int = 3   # …for this many entries
    min_multiplier: float = 0.25         # floor for combined multipliers
    journal_path: str = "state/journal.csv"
    state_path: str = "state/overlay_state.json"


@dataclass
class ValidationParams:
    """The validation battery (DESIGN.md §7): bootstrap, deflated Sharpe,
    synthetic-regime sweep. Seeds and block lengths live here so reported
    confidence intervals are reproducible too."""

    train_days: int = 90
    test_days: int = 30
    top_k: int = 3
    bootstrap_reps: int = 10000
    bootstrap_block_bars: int = 168      # ≈ 1 week of 60m bars
    journal_horizon_days: int = 5
    synthetic_accuracies: list[float] = field(
        default_factory=lambda: [0.5, 0.6, 0.7, 0.8])
    synthetic_schedules: int = 200


@dataclass
class Config:
    symbol: str = "NEARUSDT"
    base_interval: str = "1m"       # Binance klines are fetched as 1m…
    aggregate_minutes: int = 60     # …and aggregated to the trading timeframe

    paper_trading: bool = True
    testnet_url: str = "https://testnet.binance.vision"

    strategy: ScoreParams = field(default_factory=ScoreParams)
    risk: RiskParams = field(default_factory=RiskParams)
    costs: CostParams = field(default_factory=CostParams)
    data: DataParams = field(default_factory=DataParams)
    overlay: OverlayParams = field(default_factory=OverlayParams)
    validation: ValidationParams = field(default_factory=ValidationParams)

    # Secrets — populated from the environment, never from YAML.
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        raw: dict[str, Any] = {}
        p = Path(path)
        if p.exists():
            if yaml is None:
                raise RuntimeError("PyYAML is required to read config.yaml")
            with p.open() as fh:
                raw = yaml.safe_load(fh) or {}

        strategy = ScoreParams(**raw.pop("strategy", {}) or {})
        risk = RiskParams(**raw.pop("risk", {}) or {})
        costs = CostParams(**raw.pop("costs", {}) or {})
        data_params = DataParams(**raw.pop("data", {}) or {})
        overlay = OverlayParams(**raw.pop("overlay", {}) or {})
        validation = ValidationParams(**raw.pop("validation", {}) or {})

        cfg = cls(strategy=strategy, risk=risk, costs=costs, data=data_params,
                  overlay=overlay, validation=validation, **raw)
        cfg.api_key = os.getenv("BINANCE_API_KEY", "")
        cfg.api_secret = os.getenv("BINANCE_API_SECRET", "")
        return cfg

    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("api_key", None)
        d.pop("api_secret", None)
        return d

"""Indicators as pure, causal, vectorised functions.

Every function here uses only past and current data (rolling / ewm / shift),
so computing them once over a full series and slicing windows afterwards
leaks no future information into the past. That property is what makes the
walk-forward validator trustworthy, and it is pinned by the test suite.

v2 notes
--------
The v1 toolbox (SMA crossover, RSI band, per-bar volume spike) produced a
binary "all conditions green" entry. Binary gates are brittle: a parameter
nudged one step flips a trade from on to off, which is exactly the signature
of an overfit system. v2 indicators are therefore *continuous* and, where
possible, **volatility-normalised**, so they mean the same thing in a quiet
week and in a crash.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (span convention)."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range with Wilder smoothing."""
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def log_returns(close: pd.Series) -> pd.Series:
    """Bar-over-bar log returns (first value NaN)."""
    return np.log(close / close.shift(1))


def realized_vol(close: pd.Series, window: int) -> pd.Series:
    """Per-bar volatility: rolling std of log returns.

    This is the denominator for both the momentum z-scores and the
    volatility-targeting position size.
    """
    return log_returns(close).rolling(window, min_periods=window).std()


def tsmom(close: pd.Series, lookback: int, vol_window: int) -> pd.Series:
    """Volatility-normalised time-series momentum.

    ``ret(lookback) / (per-bar vol * sqrt(lookback))`` — the return over the
    lookback expressed in units of the noise expected over the same horizon
    under a random walk. +1 means "one sigma of trend". This is the standard
    CTA trend measure (Moskowitz, Ooi & Pedersen time-series momentum) and is
    comparable across regimes, assets and timeframes, unlike a raw return.
    """
    ret = close / close.shift(lookback) - 1.0
    vol = realized_vol(close, vol_window) * np.sqrt(lookback)
    return ret / vol.replace(0.0, np.nan)


def efficiency_ratio(close: pd.Series, window: int) -> pd.Series:
    """Kaufman Efficiency Ratio in [0, 1]: |net move| / path length.

    1 = perfectly straight trend, ~0 = pure chop. Used as a *regime gate*:
    trend-following signals are worthless in chop, so the strategy refuses to
    trade when recent price action was mostly back-and-forth.
    """
    net = (close - close.shift(window)).abs()
    path = close.diff().abs().rolling(window, min_periods=window).sum()
    return net / path.replace(0.0, np.nan)


def donchian_position(high: pd.Series, low: pd.Series, close: pd.Series, window: int) -> pd.Series:
    """Where the close sits inside the last ``window`` bars' range, in [-1, 1].

    +1 = closing at the window high (breakout), -1 = at the low. A continuous
    version of the classic Donchian breakout, so it blends into a score
    instead of firing a one-off event.
    """
    hh = high.rolling(window, min_periods=window).max()
    ll = low.rolling(window, min_periods=window).min()
    span = (hh - ll).replace(0.0, np.nan)
    return (2.0 * (close - ll) / span - 1.0).clip(-1.0, 1.0)


def order_flow_imbalance(taker_buy_volume: pd.Series, volume: pd.Series) -> pd.Series:
    """Signed taker imbalance per bar in [-1, 1].

    Binance klines expose ``taker_buy_base``: volume initiated by market
    *buyers*. ``ofi = 2 * taker_buy / volume - 1`` is the closest thing to
    genuine microstructure information available for free.
    """
    buy_ratio = (taker_buy_volume / volume.replace(0.0, np.nan)).clip(0.0, 1.0)
    return (2.0 * buy_ratio - 1.0).clip(-1.0, 1.0)


def ofi_zscore(taker_buy_volume: pd.Series, volume: pd.Series, window: int) -> pd.Series:
    """Smoothed order-flow imbalance, normalised by its own recent dispersion.

    Raw per-bar OFI is noisy; an EMA extracts the persistent component
    (informed traders slice large orders over many bars). Dividing by the
    rolling std keeps the signal in a comparable range across regimes.
    """
    ofi = order_flow_imbalance(taker_buy_volume, volume)
    smooth = ofi.ewm(span=window, adjust=False, min_periods=window).mean()
    disp = ofi.rolling(window, min_periods=window).std()
    return (smooth / disp.replace(0.0, np.nan)).clip(-3.0, 3.0)


def bars_per_year(bar_minutes: int) -> float:
    """Number of bars per calendar year (crypto trades 24/7)."""
    return 365.0 * 24.0 * 60.0 / bar_minutes


def add_all(df: pd.DataFrame, p) -> pd.DataFrame:
    """Attach every indicator the v2 score needs, given ScoreParams ``p``.

    Expects columns: open, high, low, close, volume, taker_buy_volume.
    Returns a copy; the input frame is not mutated. All columns are causal.
    """
    out = df.copy()
    out["vol_bar"] = realized_vol(out["close"], p.vol_window)
    out["mom_fast"] = tsmom(out["close"], p.mom_fast, p.vol_window)
    out["mom_slow"] = tsmom(out["close"], p.mom_slow, p.vol_window)
    out["breakout"] = donchian_position(out["high"], out["low"], out["close"], p.bo_window)
    out["ofi_z"] = ofi_zscore(out["taker_buy_volume"], out["volume"], p.ofi_window)
    out["er"] = efficiency_ratio(out["close"], p.er_window)
    out["trend_ma"] = sma(out["close"], p.trend_window)
    out["atr"] = atr(out["high"], out["low"], out["close"], p.atr_period)
    return out


def warmup_bars(p) -> int:
    """Bars before the first valid score — used by the live loop and tests."""
    return max(p.trend_window, p.mom_slow, p.vol_window, p.bo_window,
               p.er_window, p.ofi_window, p.atr_period) + p.mom_slow + 2

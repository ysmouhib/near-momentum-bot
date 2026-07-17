"""Shared test fixtures: deterministic synthetic 1-minute klines.

The generator alternates *trending* and *choppy* regimes (unlike v1's uniform
random walk) so the regime gates and the trend filter have something real to
switch on. Taker-buy share leans with bar direction, mimicking real order
flow. Deterministic seed => reproducible tests.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def make_1m_klines(n: int = 12000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.004, n)
    # Regime blocks of ~1500 minutes: trend up / chop / trend down / chop.
    block = 1500
    for bi, start in enumerate(range(0, n, block)):
        regime = bi % 4
        end = min(n, start + block)
        if regime == 0:
            steps[start:end] += 0.0012          # uptrend
        elif regime == 2:
            steps[start:end] -= 0.0010          # downtrend
        else:
            steps[start:end] *= 0.4             # chop: damped noise
    price = 3.0 * np.exp(np.cumsum(steps))

    close = price
    open_ = np.concatenate([[price[0]], price[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.001, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.001, n)))
    volume = rng.lognormal(9, 0.5, n)

    up = (close > open_).astype(float)
    buy_share = np.clip(0.45 + 0.15 * up + rng.normal(0, 0.05, n), 0.05, 0.95)
    taker_buy_base = volume * buy_share

    open_time = pd.date_range("2026-01-01", periods=n, freq="1min")
    return pd.DataFrame(
        {
            "open_time": open_time,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "quote_volume": volume * close,
            "trades": rng.integers(20, 500, n),
            "taker_buy_base": taker_buy_base,
            "taker_buy_quote": taker_buy_base * close,
        }
    )


@pytest.fixture
def klines_1m():
    return make_1m_klines()

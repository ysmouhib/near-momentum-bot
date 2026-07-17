"""Paper / live trading loop — the thinnest possible layer over the tested pieces.

Each cycle it fetches recent 1m klines (paginated: the v2 warmup at 60m needs
~12k 1m bars), aggregates, drops the unfinished bar, computes the score on the
last **closed** bar, and manages at most one long position:

* **Entry** when the score crosses ``theta_in``, sized by the same volatility
  target the backtester uses, against the *actual* free USDT balance.
* **Exit** on score fade (hysteresis), the chandelier trailing stop, or the
  time stop — the same rules, in the same order, as the backtest loop.

Because signal, sizing and exit maths are shared with the backtester, live
behaviour matches simulated behaviour as closely as market noise allows.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import data, indicators
from . import score as score_mod

logger = logging.getLogger(__name__)


def _fill_price(order) -> float | None:
    """Volume-weighted average fill price from a market-order response.

    Testnet/live spot responses include a ``fills`` list; using it closes the
    gap between the price we *log* and the price we actually *got*.
    """
    if not order:
        return None
    fills = order.get("fills") or []
    total_qty = sum(float(f["qty"]) for f in fills)
    if total_qty <= 0:
        return None
    return sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty


@dataclass
class Position:
    qty: float
    entry_price: float
    weight: float
    peak_close: float
    entry_bar_time: object
    bars_held: int = 0


class Executor:
    def __init__(self, cfg, broker):
        self.cfg = cfg
        self.broker = broker
        self.position: Position | None = None
        self.last_bar_time = None
        self.cooldown_until_bar = 0
        self._bars_seen = 0
        self.closed_trades: list[dict] = []

    def _fetch_frame(self):
        p = self.cfg.strategy
        agg = self.cfg.aggregate_minutes
        need_agg = indicators.warmup_bars(p) + 5
        klines = self.broker.get_klines_history(self.cfg.base_interval,
                                                min(20000, need_agg * agg + agg))
        if not klines:
            return None
        df_1m = data.klines_to_df(klines)
        bars = data.aggregate(df_1m, agg, drop_incomplete=True)
        if len(bars) < indicators.warmup_bars(p) + 2:
            return None
        frame = indicators.add_all(bars, p)
        frame["score"] = score_mod.compute(frame, p)
        return frame

    # ------------------------------------------------------------------ exits
    def _manage_position(self, row, live_price: float):
        pos = self.position
        p = self.cfg.strategy
        close = float(row["close"])
        pos.peak_close = max(pos.peak_close, close)

        reason = None
        if float(row["score"]) < p.theta_out:
            reason = "score_fade"
        elif close < pos.peak_close - p.trail_atr * float(row["atr"]):
            reason = "trail_stop"
        elif pos.bars_held >= p.max_hold_bars:
            reason = "time_stop"

        if reason:
            order = self.broker.sell(pos.qty, price_hint=live_price)
            if order is None:
                return
            exit_px = _fill_price(order) or live_price
            pnl = (exit_px / pos.entry_price - 1.0 - 2 * self.cfg.costs.taker_fee) * pos.weight
            logger.info("EXIT (%s) @ %.4f | est. portfolio P&L %+.3f%%", reason, exit_px, pnl * 100)
            self.closed_trades.append(
                {"entry": pos.entry_price, "exit": exit_px, "reason": reason,
                 "weight": pos.weight, "weighted_pct": pnl})
            self.position = None
            self.cooldown_until_bar = self._bars_seen + self.cfg.risk.cooldown_bars

    # ----------------------------------------------------------------- entry
    def _maybe_enter(self, row, live_price: float):
        if self._bars_seen < self.cooldown_until_bar:
            return
        p = self.cfg.strategy
        s = float(row["score"])
        if s <= p.theta_in:
            logger.debug("No entry: score %.3f <= theta_in %.2f", s, p.theta_in)
            return
        from .backtest import position_weight  # same sizing as the backtester

        w = position_weight(float(row["vol_bar"]), self.cfg.aggregate_minutes,
                            p.target_vol, p.min_weight)
        if w <= 0.0:
            logger.info("Entry skipped: vol target sizes below min_weight")
            return
        free_usdt, _ = self.broker.balance("USDT")
        notional = min(w, self.cfg.risk.max_notional_frac) * free_usdt
        qty = notional / live_price
        order = self.broker.buy(qty, price_hint=live_price)
        if order is None:
            return
        fill = _fill_price(order) or live_price
        real_qty = self.broker.round_qty(qty)
        self.position = Position(
            qty=real_qty, entry_price=fill, weight=w,
            peak_close=float(row["close"]), entry_bar_time=row["timestamp"],
        )
        logger.info("ENTER long @ ~%.4f | weight %.2f | %s", fill, w,
                    score_mod.explain(row, s, p))

    # ------------------------------------------------------------------ loop
    def step(self):
        """Run a single decision cycle. Separated out so it is unit-testable."""
        frame = self._fetch_frame()
        if frame is None:
            logger.info("Not enough data yet; waiting.")
            return
        row = frame.iloc[-1]
        new_bar = row["timestamp"] != self.last_bar_time
        if new_bar:
            self._bars_seen += 1
            self.last_bar_time = row["timestamp"]
            if self.position:
                self.position.bars_held += 1

        live_price = self.broker.price()
        if self.position:
            self._manage_position(row, live_price)
        if self.position is None:
            self._maybe_enter(row, live_price)

        logger.info(
            "price=%.4f score=%+.3f | in_position=%s | closed_trades=%d",
            live_price, float(row["score"]) if row["score"] == row["score"] else float("nan"),
            self.position is not None, len(self.closed_trades),
        )

    def run(self, poll_seconds: int = 30):
        mode = "PAPER (testnet)" if self.cfg.paper_trading else "LIVE"
        logger.info("Starting executor in %s mode on %s. Ctrl+C to stop.", mode, self.cfg.symbol)
        try:
            while True:
                start = time.time()
                try:
                    self.step()
                except Exception as exc:  # keep the loop alive on transient errors
                    logger.exception("Cycle error: %s", exc)
                time.sleep(max(1, poll_seconds - (time.time() - start)))
        except KeyboardInterrupt:
            logger.info("Stopped by user. Closed trades: %d", len(self.closed_trades))

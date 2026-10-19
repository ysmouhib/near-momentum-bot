"""Paper / live trading loop — the thinnest possible layer over the tested pieces.

Each cycle it fetches recent 1m klines (paginated: the v2 warmup at 60m needs
~12k 1m bars), aggregates, drops the unfinished bar, computes the score on the
last **closed** bar, and manages at most one long position:

* **Entry** when the score crosses ``theta_in``, sized by the same volatility
  target the backtester uses — and, with the overlay enabled, by the same
  risk-per-trade budget, exposure caps and kill-switch multipliers the
  backtester's overlay path applies. What you backtest is what you run.
* **Exit** on score fade (hysteresis), the chandelier trailing stop, or the
  time stop — the same rules, in the same order, as the backtest loop. The
  overlay's full stop is the only additional exit, and it belongs to the risk
  policy, not to the human.

Overlay integration (docs/DESIGN.md §4.5)
-----------------------------------------
* DISARMED blocks entries only; an open position keeps its engine exits.
* The kill switch is evaluated once per cycle on mark-to-market equity
  (``broker.equity_usdt``), *before* any entry logic; auto events are
  journaled and the state file persisted.
* **Fail closed**: if overlay evaluation itself raises, the cycle behaves as
  DISARMED (no entries). A risk overlay that crashes open is worse than none.
* **Restart reconciliation**: the open position and equity anchors live in
  ``state/overlay_state.json``, written at every fill. On startup the state
  is checked against actual exchange balances — a state/exchange mismatch
  (e.g. a manually placed order) blocks entries until the human resolves it,
  because absorbing a discrepancy silently is how risk systems lie.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import data, indicators
from . import overlay as ov
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
    m_tier: float = 1.0
    m_cooldown: float = 1.0

    def to_dict(self) -> dict:
        return {"qty": self.qty, "entry_price": self.entry_price,
                "weight": self.weight, "peak_close": self.peak_close,
                "entry_bar_time": str(self.entry_bar_time),
                "bars_held": self.bars_held,
                "m_tier": self.m_tier, "m_cooldown": self.m_cooldown}


class Executor:
    def __init__(self, cfg, broker):
        self.cfg = cfg
        self.broker = broker
        self.position: Position | None = None
        self.last_bar_time = None
        self.cooldown_until_bar = 0
        self._bars_seen = 0
        self.closed_trades: list[dict] = []
        self._entries_blocked = False  # set by failed restart reconciliation
        self.ov_state: ov.OverlayState | None = None
        if cfg.overlay.enabled:
            self.ov_state = ov.load_state(cfg.overlay.state_path)
            self._reconcile()

    # ---------------------------------------------------------- persistence
    def _persist(self):
        if self.ov_state is not None:
            self.ov_state.open_position = (
                self.position.to_dict() if self.position else None)
            ov.save_state(self.ov_state, self.cfg.overlay.state_path)

    def _journal(self, event: str, note: str, equity: float | str = "",
                 price: float | str = "", context: str = ""):
        from . import journal

        journal.append(self.cfg.overlay.journal_path, event,
                       self.ov_state.state if self.ov_state else "n/a", note,
                       equity=equity, price=price,
                       hwm=round(self.ov_state.hwm, 4) if self.ov_state else "",
                       context=context)

    # --------------------------------------------------------- reconciliation
    def _reconcile(self):
        """Match the persisted state against actual exchange balances.

        State says long + exchange holds base -> reconstruct the position.
        State says long + exchange flat -> the position vanished (manual
        intervention?); clear it and carry on. State flat + exchange holds
        non-dust base -> something traded outside the bot; block entries
        until the human resolves it.
        """
        base = self.cfg.symbol.replace("USDT", "")
        try:
            free_base, locked_base = self.broker.balance(base)
            held = free_base + locked_base
            price = self.broker.price()
        except Exception as exc:
            logger.error("Reconciliation read failed (%s) — entries blocked", exc)
            self._entries_blocked = True
            return
        dust = (self.broker.min_notional or 0.0) / price if price else 0.0
        saved = self.ov_state.open_position
        if saved and held > dust:
            self.position = Position(
                qty=min(saved["qty"], held) if saved.get("qty") else held,
                entry_price=saved["entry_price"], weight=saved["weight"],
                peak_close=saved["peak_close"],
                entry_bar_time=saved.get("entry_bar_time"),
                bars_held=saved.get("bars_held", 0),
                m_tier=saved.get("m_tier", 1.0), m_cooldown=saved.get("m_cooldown", 1.0))
            logger.info("Reconciled open position from state: %s", self.position)
        elif saved:
            logger.warning("State had an open position but the exchange holds no "
                           "%s — clearing (manual intervention?)", base)
            self.position = None
            self._persist()
        elif held > dust:
            logger.critical("Exchange holds %.6f %s but the state file has no open "
                            "position — entries blocked until resolved", held, base)
            self._entries_blocked = True

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

    # -------------------------------------------------------------- overlay
    def _overlay_cycle(self, row, live_price: float) -> bool:
        """Evaluate the kill switch for this cycle. Returns entries-allowed.

        Runs before any entry logic; auto events are journaled; failures
        fail closed (entries blocked for this cycle).
        """
        if self.ov_state is None:
            return True
        p = self.cfg.overlay
        date_str = str(row["timestamp"])[:10]
        try:
            equity = self.broker.equity_usdt()
            events = ov.evaluate(self.ov_state, p, equity, date_str)
            for event, note in events:
                logger.warning("OVERLAY %s: %s", event, note)
                self._journal(event, note, equity=round(equity, 2), price=live_price)
            if events:
                self._persist()
        except Exception:  # noqa: BLE001 - fail closed on *any* overlay error
            logger.exception("Overlay evaluation failed — failing closed")
            return False
        if ov.should_flatten(self.ov_state) and self.position:
            self._flatten(live_price, equity)
        return self.ov_state.state == ov.ARMED and self.ov_state.halted_date != date_str

    def _flatten(self, live_price: float, equity: float):
        """The one overlay-forced exit: sell everything at market."""
        pos = self.position
        order = self.broker.sell(pos.qty, price_hint=live_price)
        if order is None:
            logger.error("Full-stop flatten order failed — retrying next cycle")
            return
        exit_px = _fill_price(order) or live_price
        pnl = (exit_px / pos.entry_price - 1.0 - 2 * self.cfg.costs.taker_fee) * pos.weight
        logger.warning("FULL STOP FLATTEN @ %.4f | est. portfolio P&L %+.3f%%",
                       exit_px, pnl * 100)
        self._journal("auto_flatten", "full stop: position flattened at market",
                      equity=round(equity, 2), price=exit_px,
                      context=f"weighted_pct={pnl:.6f}")
        self._record_close(pos, exit_px, "full_stop", pnl)

    # ------------------------------------------------------------------ exits
    def _record_close(self, pos: Position, exit_px: float, reason: str, pnl: float):
        self.closed_trades.append(
            {"entry": pos.entry_price, "exit": exit_px, "reason": reason,
             "weight": pos.weight, "weighted_pct": pnl})
        if self.ov_state is not None:
            for event, note in ov.on_trade_closed(self.ov_state):
                self._journal(event, note)
        self.position = None
        self.cooldown_until_bar = self._bars_seen + self.cfg.risk.cooldown_bars
        self._persist()

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
            if self.ov_state is not None:
                self._journal("trade", f"exit {reason}", price=exit_px,
                              context=f"weighted_pct={pnl:.6f} entry={pos.entry_price}")
            self._record_close(pos, exit_px, reason, pnl)

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

        m_tier = m_cool = 1.0
        if self.ov_state is not None:
            op = self.cfg.overlay
            mult = ov.multiplier(self.ov_state, op)
            equity = self.broker.equity_usdt()
            w = ov.entry_weight(w, live_price, float(row["atr"]), p.trail_atr,
                                0.0, mult, op, p.min_weight)
            if w <= 0.0:
                logger.info("Entry skipped: overlay sizing below min_weight")
                return
            m_tier = ov.TIER_MULT if self.ov_state.half_tier else 1.0
            m_cool = op.transition_size_mult if self.ov_state.cooldown_left > 0 else 1.0
            free_usdt, _ = self.broker.balance("USDT")
            notional = min(w * equity, self.cfg.risk.max_notional_frac * free_usdt)
        else:
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
            m_tier=m_tier, m_cooldown=m_cool)
        self._persist()
        logger.info("ENTER long @ ~%.4f | weight %.2f (x%.2f) | %s", fill, w,
                    m_tier * m_cool, score_mod.explain(row, s, p))

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
        entries_allowed = self._overlay_cycle(row, live_price)
        if self.position:
            self._manage_position(row, live_price)
        if self.position is None and entries_allowed and not self._entries_blocked:
            self._maybe_enter(row, live_price)

        logger.info(
            "price=%.4f score=%+.3f | in_position=%s | state=%s | closed_trades=%d",
            live_price, float(row["score"]) if row["score"] == row["score"] else float("nan"),
            self.position is not None,
            self.ov_state.state if self.ov_state else "engine-only",
            len(self.closed_trades),
        )

    def run(self, poll_seconds: int = 30):
        mode = "PAPER (testnet)" if self.cfg.paper_trading else "LIVE"
        logger.info("Starting executor in %s mode on %s. Ctrl+C to stop.", mode, self.cfg.symbol)
        if self.ov_state is not None:
            logger.info("Overlay state: %s | multiplier x%.2f | cooldown left %d",
                        self.ov_state.state, ov.multiplier(self.ov_state, self.cfg.overlay),
                        self.ov_state.cooldown_left)
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

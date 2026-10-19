"""Event-driven backtester with exact per-bar accounting and honest costs.

Realism choices (each one makes results *worse* and more trustworthy):

* Signals are computed on a **closed** bar; fills happen at the **next** bar's
  open. No decision uses information from the bar it trades on.
* Every fill pays a taker fee and slippage, on entry and on exit.
* The position is **marked to market every bar**, so equity, drawdown, Sharpe
  and exposure reflect the ride between entry and exit — not just closed
  trades. Per-bar returns are accounted exactly once (see _run_loop).
* Sizing is **volatility-targeted**: the weight is fixed at entry to make the
  position's annualised vol equal ``target_vol`` (capped at 100% notional).
  Quiet markets get bigger positions, wild markets smaller ones — which is
  what keeps a trend system's equity curve from being hostage to vol regime.

Exit rule = first of: score fading below ``theta_out`` (hysteresis), a
chandelier trailing stop (peak close minus ``trail_atr`` ATRs), or a time
stop. Entry rule = score above ``theta_in``. ``theta_in > theta_out`` is the
no-trade band that stops the score from churning fees around the threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import indicators, score as score_mod


@dataclass
class Trade:
    entry_time: object
    exit_time: object
    entry_price: float
    exit_price: float
    weight: float           # fraction of capital allocated (vol-targeted)
    reason_out: str
    gross_pct: float        # price move between fills (slippage embedded)
    net_pct: float          # gross minus fees, on the position
    weighted_net: float     # net_pct * weight — contribution to portfolio
    bars_held: int
    reason_in: str = ""
    m_tier: float = 1.0     # kill-switch size multiplier at entry (overlay)
    m_cooldown: float = 1.0 # transition-cooldown multiplier at entry (overlay)


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    position: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    bar_returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    metrics: dict = field(default_factory=dict)

    def summary(self) -> str:
        m = self.metrics
        if not m or m.get("n_trades", 0) == 0:
            return "No trades were taken over this window."
        lines = [
            f"Trades            : {m['n_trades']}",
            f"Win rate          : {m['win_rate']:.1%}",
            f"Expectancy/trade  : {m['expectancy_weighted']:+.4%} weighted "
            f"(unweighted {m['expectancy']:+.4%})",
            f"  gross / drag    : {m['expectancy_gross']:+.4%} / {m['cost_drag']:.4%}",
            f"Profit factor     : {m['profit_factor']:.2f}",
            f"Total return      : {m['total_return']:+.2%}  (buy&hold {m['buy_hold']:+.2%})",
            f"Annualised return : {m['ann_return']:+.2%}",
            f"Annualised vol    : {m['ann_vol']:.2%}",
            f"Sharpe (annual)   : {m['sharpe']:+.2f}",
            f"Sortino (annual)  : {m['sortino']:+.2f}",
            f"Max drawdown      : {m['max_drawdown']:.2%}",
            f"Calmar            : {m['calmar']:+.2f}",
            f"Exposure          : {m['exposure']:.1%} of bars",
            f"Turnover          : {m['turnover']:.1f}x capital",
            f"Avg bars held     : {m['avg_bars_held']:.1f}",
        ]
        return "\n".join(lines)


def position_weight(vol_bar: float, bar_minutes: int, target_vol: float,
                    min_weight: float, max_weight: float = 1.0) -> float:
    """Volatility-target weight for one entry.

    ``w = target_vol / annualised_vol`` capped to [0, max_weight]. Returns 0.0
    when the target cannot be reached at or above ``min_weight`` (dust trades
    are not worth the ticket).
    """
    if not np.isfinite(vol_bar) or vol_bar <= 0:
        return 0.0
    ann_vol = vol_bar * np.sqrt(indicators.bars_per_year(bar_minutes))
    w = min(max_weight, target_vol / ann_vol)
    return w if w >= min_weight else 0.0


def run(frame: pd.DataFrame, cfg, score: pd.Series | None = None,
        overlay=None, armed: pd.Series | None = None) -> BacktestResult:
    """Backtest over ``frame`` (output of ``indicators.add_all``).

    ``score`` may be precomputed (walk-forward reuses it across configs); it is
    recomputed from ``cfg.strategy`` otherwise. The frame must be sorted by
    time ascending.

    ``overlay`` (an ``OverlayParams``) activates the kill-switch ladder,
    risk-based sizing and permission gating from ``overlay.py``; ``armed`` is a
    boolean per-bar permission series (a human or synthetic regime schedule).
    Both default off: with neither given, the loop is byte-identical to the
    pure engine, which is what keeps the JS parity fixtures valid.
    """
    p = cfg.strategy
    costs = cfg.costs
    if score is None:
        score = score_mod.compute(frame, p)
    if (overlay is not None or armed is not None) and "timestamp" not in frame.columns:
        raise ValueError("overlay/armed runs require a 'timestamp' column")

    rows = frame.reset_index(drop=True)
    n = len(rows)
    a_open = rows["open"].to_numpy(dtype=float)
    a_high = rows["high"].to_numpy(dtype=float)
    a_low = rows["low"].to_numpy(dtype=float)
    a_close = rows["close"].to_numpy(dtype=float)
    a_vol = rows["vol_bar"].to_numpy(dtype=float)
    a_atr = rows["atr"].to_numpy(dtype=float)
    a_score = score.to_numpy(dtype=float)
    a_armed = (np.ones(n, dtype=bool) if armed is None
               else armed.reset_index(drop=True).to_numpy(dtype=bool))
    if len(a_armed) != n:
        raise ValueError("armed series must align with frame")
    times = rows["timestamp"].tolist() if "timestamp" in rows.columns else list(range(n))

    fee, slip = costs.taker_fee, costs.slippage
    ret, pos_arr, ov_stats = _run_loop(
        a_open, a_high, a_low, a_close, a_vol, a_atr, a_score, times,
        p, fee, slip, cfg.aggregate_minutes,
        trades_out := [], overlay, a_armed,
    )
    equity = pd.Series(np.cumprod(1.0 + ret), index=rows.index, name="equity")
    bar_returns = pd.Series(ret, index=rows.index, name="bar_return")
    position = pd.Series(pos_arr, index=rows.index, name="weight")
    metrics = _metrics(trades_out, ret, pos_arr, a_close, cfg.aggregate_minutes)
    if overlay is not None or armed is not None:
        metrics.update(ov_stats)
    return BacktestResult(trades_out, equity, position, bar_returns, metrics)


def _run_loop(a_open, a_high, a_low, a_close, a_vol, a_atr, a_score, times,
              p, fee, slip, bar_minutes, trades, ov_params=None, a_armed=None):
    """The trading loop. Returns (per-bar returns, per-bar weights, overlay stats).

    Accounting convention — every bar's return is written exactly once:
      * entry bar:   w * (close/fill_in - 1) - w*fee
      * holding bar: w * (close/prev_close - 1)
      * exit bar:    w * (fill_out/prev_close - 1) - w*fee
    Slippage is inside the fill prices; fees are charged on both sides.

    With ``ov_params`` None the loop is the pure v2 engine. With an overlay,
    two extra mechanisms act *around* the same accounting: permission
    (``ov.can_enter`` — the human/kill-switch gate) and sizing composition
    (``ov.entry_weight``). Equity for the kill switch is tracked by folding
    each bar's return into a running product exactly once (``fold``), under
    the same convention as the returns themselves.
    """
    from . import overlay as ov

    n = len(a_close)
    ret = np.zeros(n)
    pos_arr = np.zeros(n)
    w = 0.0
    entry_bar = -1
    peak = 0.0
    fill_in = 0.0
    reason_in = ""
    m_tier_in = 1.0
    m_cool_in = 1.0
    last_counted = -1  # highest bar index whose return has been written

    ov_state = ov.OverlayState(state=ov.ARMED) if ov_params is not None else None
    ov_active = ov_params is not None or a_armed is not None
    dates = [str(t)[:10] for t in times] if ov_active else None
    eq = 1.0          # running mark-to-market equity for the kill switch
    eq_folded = 0     # bars <= eq_folded are already inside eq
    blocked = halts = full_stops = 0

    def fold(j):
        nonlocal eq, eq_folded
        while eq_folded < j:
            eq_folded += 1
            eq *= 1.0 + ret[eq_folded]

    def ov_events(date_i):
        nonlocal halts, full_stops
        events = ov.evaluate(ov_state, ov_params, eq, date_i)
        halts += sum(1 for e, _ in events if e == "auto_halt_day")
        full_stops += sum(1 for e, _ in events if e == "auto_full_stop")

    def close_trade(exit_bar, fill_out, reason, held):
        gross = fill_out / fill_in - 1.0
        net = gross - 2.0 * fee
        trades.append(Trade(
            entry_time=times[entry_bar], exit_time=times[exit_bar],
            entry_price=fill_in, exit_price=fill_out, weight=w,
            reason_out=reason, gross_pct=gross, net_pct=net,
            weighted_net=net * w, bars_held=held, reason_in=reason_in,
            m_tier=m_tier_in, m_cooldown=m_cool_in,
        ))
        if ov_params is not None:
            ov.on_trade_closed(ov_state)

    i = 1
    while i < n - 1:
        if w == 0.0:
            if ov_active:
                fold(i - 1)
                if ov_params is not None:
                    ov_events(dates[i])
            s = a_score[i]
            if np.isfinite(s) and s > p.theta_in:
                if ov_params is not None:
                    permitted = ov.can_enter(ov_state, dates[i], bool(a_armed[i]))
                elif a_armed is not None:
                    permitted = bool(a_armed[i])
                else:
                    permitted = True
                if not permitted:
                    blocked += 1
                    i += 1
                    continue
                w_new = position_weight(a_vol[i], bar_minutes, p.target_vol, p.min_weight)
                if ov_params is not None and w_new > 0.0:
                    mult = ov.multiplier(ov_state, ov_params)
                    w_new = ov.entry_weight(
                        w_new, a_open[i + 1], a_atr[i], p.trail_atr,
                        0.0, mult, ov_params, p.min_weight)
                if w_new > 0.0:
                    if ov_params is not None:
                        m_tier_in = ov.TIER_MULT if ov_state.half_tier else 1.0
                        m_cool_in = (ov_params.transition_size_mult
                                     if ov_state.cooldown_left > 0 else 1.0)
                    w = w_new
                    entry_bar = i + 1
                    fill_in = a_open[entry_bar] * (1.0 + slip)
                    peak = a_close[entry_bar]
                    ret[entry_bar] += w * (a_close[entry_bar] / fill_in - 1.0) - w * fee
                    pos_arr[entry_bar] = w
                    last_counted = entry_bar
                    row = i  # reason string uses the signal bar
                    reason_in = f"score={a_score[row]:+.2f} w={w:.2f}"
                    i += 1
                    continue
            i += 1
            continue

        # ---- in position at close of bar i ----
        if i > entry_bar:
            # entry-bar return was already written at the fill; later bars
            # accrue close-to-close
            ret[i] += w * (a_close[i] / a_close[i - 1] - 1.0)
            last_counted = i
        pos_arr[i] = w
        peak = max(peak, a_close[i])
        if ov_params is not None:
            fold(i)
            ov_events(dates[i])

        exit_reason = None
        if ov_params is not None and ov.should_flatten(ov_state):
            exit_reason = "full_stop"
        else:
            s = a_score[i]
            if np.isfinite(s) and s < p.theta_out:
                exit_reason = "score_fade"
            elif a_close[i] < peak - p.trail_atr * a_atr[i]:
                exit_reason = "trail_stop"
            elif (i - entry_bar + 1) >= p.max_hold_bars:
                exit_reason = "time_stop"

        if exit_reason is not None:
            exit_bar = i + 1
            fill_out = a_open[exit_bar] * (1.0 - slip)
            ret[exit_bar] += w * (fill_out / a_close[i] - 1.0) - w * fee
            close_trade(exit_bar, fill_out, exit_reason, exit_bar - entry_bar)
            w = 0.0
            i += 1
            continue
        i += 1

    # Mark-to-market any position still open when the data ends.
    if w > 0.0:
        exit_bar = n - 1
        fill_out = a_close[exit_bar] * (1.0 - slip)
        if last_counted == exit_bar:
            ret[exit_bar] -= w * (fee + slip)  # bar return already written
        else:
            ret[exit_bar] += w * (fill_out / a_close[exit_bar - 1] - 1.0) - w * fee
        close_trade(exit_bar, fill_out, "end_of_data", exit_bar - entry_bar + 1)
    return ret, pos_arr, {"n_blocked_entries": blocked, "n_day_halts": halts,
                          "n_full_stops": full_stops}


def _metrics(trades: list[Trade], ret: np.ndarray, pos_arr: np.ndarray,
             close: np.ndarray, bar_minutes: int) -> dict:
    bpy = indicators.bars_per_year(bar_minutes)
    equity = np.cumprod(1.0 + ret)
    running_max = np.maximum.accumulate(equity)
    drawdown = equity / running_max - 1.0
    max_dd = float(drawdown.min())

    mu, sd = float(ret.mean()), float(ret.std())
    downside = ret[ret < 0]
    sd_down = float(downside.std()) if len(downside) > 1 else 0.0
    n_bars = len(ret)
    # geometric annualisation in log space (plain ** overflows on long runs)
    if n_bars and equity[-1] > 0:
        ann_return = float(np.exp(np.log(equity[-1]) * bpy / n_bars) - 1.0)
    else:
        ann_return = -1.0 if n_bars else 0.0

    m = {
        "total_return": float(equity[-1] - 1.0),
        "ann_return": ann_return,
        "ann_vol": sd * np.sqrt(bpy),
        "sharpe": mu / sd * np.sqrt(bpy) if sd > 0 else 0.0,
        "sortino": mu / sd_down * np.sqrt(bpy) if sd_down > 0 else 0.0,
        "max_drawdown": max_dd,
        "calmar": ann_return / abs(max_dd) if max_dd < 0 else 0.0,
        "exposure": float((pos_arr > 0).mean()),
        "buy_hold": float(close[-1] / close[0] - 1.0) if n_bars > 1 else 0.0,
        "n_bars": n_bars,
    }
    if not trades:
        m.update({"n_trades": 0})
        return m

    nets = np.array([t.net_pct for t in trades])
    wnets = np.array([t.weighted_net for t in trades])
    gross_w = np.array([t.gross_pct * t.weight for t in trades])
    wins, losses = wnets[wnets > 0], wnets[wnets <= 0]
    gross_loss = abs(losses.sum())
    m.update({
        "n_trades": len(trades),
        "win_rate": float(len(wins) / len(wnets)),
        "expectancy": float(nets.mean()),
        "expectancy_weighted": float(wnets.mean()),
        "expectancy_gross": float(gross_w.mean()),
        "cost_drag": float(gross_w.mean() - wnets.mean()),
        "profit_factor": float(wins.sum() / gross_loss) if gross_loss > 0 else float("inf"),
        "turnover": float(2.0 * sum(t.weight for t in trades)),
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])),
        "avg_weight": float(np.mean([t.weight for t in trades])),
    })
    return m


# Public aliases used by walkforward/report and available to notebooks.
def metrics_from_returns(ret: pd.Series, bar_minutes: int) -> dict:
    """Equity-level metrics for an arbitrary per-bar return series."""
    pos = (ret != 0).to_numpy(dtype=float)
    close = np.array([1.0, 1.0])
    return _metrics([], ret.to_numpy(dtype=float), pos, close, bar_minutes)

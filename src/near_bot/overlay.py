"""The risk overlay: a human permission switch plus mechanical brakes.

Why this layer exists
---------------------
The v2 engine owns every trade-level decision — entries by conviction score,
exits by score fade / chandelier trailing stop / time stop. What the engine
cannot own is the *regime* question: is a directional move plausible in the
coming days at all? Trend Rider answers that with a human who arms or
disarms the bot 1–2 times per day at most, and with a kill-switch ladder
that acts when the human is wrong, asleep, or both.

Design contracts (docs/DESIGN.md §4), all enforced here:

* **DISARMED blocks new entries only.** An open position keeps its engine
  exits — disarming is not a sell button, and the human never overrides an
  individual trade.
* **The full stop is the only overlay-forced exit.** A −10% drawdown from
  the equity high-water mark means the regime thesis or the engine's
  assumptions broke; the position is flattened and a human re-arm is
  required. Everything less severe only reduces or blocks *new* exposure.
* **Hysteresis at the tier boundary.** The −5% half-size tier clears at
  −3%, so it cannot flap on every tick around the trigger.
* **Transition cooldown.** Regime-call errors cluster at transitions, so
  the first N entries after every human arm are taken at half size.

The module is deliberately pure: no I/O beyond (de)serialisation, no broker,
no logging. The same state object drives the backtest overlay and the live
executor, so what is simulated is what runs — the v2 invariant applied to
the overlay itself. Persistence uses write-tmp-then-rename so a crash
mid-write cannot truncate the state file.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

DISARMED = "disarmed"
ARMED = "armed"
STOPPED = "stopped"

TIER_MULT = 0.5  # the −5%-from-HWM tier is literally "half size"


@dataclass
class OverlayState:
    """Everything the overlay remembers between bars, cycles and restarts."""

    state: str = DISARMED
    hwm: float = 1.0                 # high-water mark of mark-to-market equity
    day_start_equity: float = 1.0    # equity at the first bar/cycle of the UTC day
    day_start_date: str = ""         # YYYY-MM-DD (UTC) of the current day anchor
    halted_date: str = ""            # UTC date under daily-loss halt ("" = none)
    half_tier: bool = False          # −5% HWM tier currently active
    cooldown_left: int = 0           # entries remaining at the transition multiplier
    open_position: dict | None = None  # persisted by the executor for restart recovery


# --------------------------------------------------------------------- events
# Every transition returns (event, note) pairs; callers append them to the
# journal (journal.py). The overlay itself never writes the journal.

def arm(state: OverlayState, p) -> list[tuple[str, str]]:
    """Human arms the bot. Re-arming while armed is a journaled no-op."""
    if state.state == ARMED:
        return [("arm", "already armed (no-op)")]
    prev = state.state
    state.state = ARMED
    state.cooldown_left = p.cooldown_trades_after_arm
    state.halted_date = ""
    return [("arm", f"armed from {prev}; half size for the next {state.cooldown_left} trades")]


def disarm(state: OverlayState) -> list[tuple[str, str]]:
    """Human disarms the bot: no new entries; open positions keep engine exits."""
    if state.state == DISARMED:
        return [("disarm", "already disarmed (no-op)")]
    prev = state.state
    state.state = DISARMED
    return [("disarm", f"disarmed from {prev}")]


def evaluate(state: OverlayState, p, equity: float, date_str: str) -> list[tuple[str, str]]:
    """Update equity anchors and kill-switch tiers for one bar/cycle.

    ``equity`` is mark-to-market (open position included); ``date_str`` is
    the bar/cycle UTC date (YYYY-MM-DD). Call once per bar in backtests and
    once per cycle live, *before* any entry logic.
    """
    events: list[tuple[str, str]] = []

    if date_str != state.day_start_date:
        state.day_start_date = date_str
        state.day_start_equity = equity
        state.halted_date = ""  # a new UTC day lifts the daily halt

    state.hwm = max(state.hwm, equity)
    dd = equity / state.hwm - 1.0 if state.hwm > 0 else 0.0

    # The deepest tier wins and short-circuits the rest.
    if dd <= p.hwm_full_stop_frac:
        if state.state != STOPPED:
            state.state = STOPPED
            state.half_tier = False
            events.append(("auto_full_stop",
                           (f"drawdown {dd:.1%} from HWM <= {p.hwm_full_stop_frac:.0%}; "
                            "flatten and wait for human re-arm")))
        return events
    if state.state == STOPPED:
        return events  # only a human arm() leaves this state

    if state.day_start_equity > 0:
        day_ret = equity / state.day_start_equity - 1.0
        if day_ret <= p.daily_loss_halt_frac and state.halted_date != date_str:
            state.halted_date = date_str
            events.append(("auto_halt_day",
                           (f"daily loss {day_ret:.1%} <= {p.daily_loss_halt_frac:.0%}; "
                            "entries blocked until next UTC day")))

    if not state.half_tier and dd <= p.hwm_half_size_frac:
        state.half_tier = True
        events.append(("auto_half_on",
                       f"drawdown {dd:.1%} <= {p.hwm_half_size_frac:.0%}: half size"))
    elif state.half_tier and dd > p.hwm_half_clear_frac:
        state.half_tier = False
        events.append(("auto_half_off",
                       f"drawdown recovered to {dd:.1%} > {p.hwm_half_clear_frac:.0%}: full size"))
    return events


def on_trade_closed(state: OverlayState) -> list[tuple[str, str]]:
    """Count a closed trade against the post-arm transition cooldown."""
    if state.cooldown_left > 0:
        state.cooldown_left -= 1
        if state.cooldown_left == 0:
            return [("cooldown_done", "transition cooldown exhausted; full size")]
    return []


# ------------------------------------------------------------------ predicates

def can_enter(state: OverlayState, date_str: str, armed_bar: bool = True) -> bool:
    """May a new position be opened right now?

    ``armed_bar`` is the per-bar permission series used by backtests (a
    synthetic human schedule); live, the state machine's own flag rules.
    """
    return state.state == ARMED and state.halted_date != date_str and armed_bar


def should_flatten(state: OverlayState) -> bool:
    """The one overlay-forced exit: full stop with a position still open."""
    return state.state == STOPPED


def multiplier(state: OverlayState, p) -> float:
    """Combined size multiplier for new entries (tier × transition cooldown)."""
    m = 1.0
    if state.half_tier:
        m *= TIER_MULT
    if state.cooldown_left > 0:
        m *= p.transition_size_mult
    return min(1.0, max(m, p.min_multiplier))


def entry_weight(w_vol: float, entry_price: float, atr: float, trail_atr: float,
                 open_exposure: float, mult: float, p, min_weight: float) -> float:
    """Overlay sizing for one entry.

    The risk budget R (a fraction of equity) divided by the distance to the
    chandelier stop the engine will actually trail gives a risk-based weight;
    the vol-target weight, the exposure cap and the hard spot cap bound it
    from above; kill-switch/cooldown multipliers shrink it. Below
    ``min_weight`` the trade is dust and skipped (0.0).
    """
    if entry_price <= 0 or atr <= 0:
        return 0.0
    stop_dist = trail_atr * atr / entry_price
    if stop_dist <= 0:
        return 0.0
    w_risk = p.per_trade_risk_frac / stop_dist
    w = min(w_vol, w_risk, p.max_exposure_frac - open_exposure, p.hard_max_weight) * mult
    return w if w >= min_weight else 0.0


# ----------------------------------------------------------------- persistence

def save_state(state: OverlayState, path: str | Path) -> None:
    """Atomic write: tmp file + rename, so a crash cannot truncate state."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.replace(tmp, path)


def load_state(path: str | Path) -> OverlayState:
    """Load state; a missing file means a fresh, disarmed overlay."""
    path = Path(path)
    if not path.exists():
        return OverlayState()
    data = json.loads(path.read_text())
    known = {f for f in OverlayState.__dataclass_fields__}
    return OverlayState(**{k: v for k, v in data.items() if k in known})

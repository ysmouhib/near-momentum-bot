"""Command-line interface: ``near-bot <command>`` or ``python -m near_bot ...``.

Subcommands
-----------
test-connection  Verify API keys and print account / symbol info.
download         Fetch 1m klines to CSV (REST API or data.binance.vision dumps).
backtest         Run the strategy over a CSV of 1-minute klines.
walkforward      Optimise on rolling in-sample windows, report out-of-sample.
validate         Multi-symbol battery: bootstrap CIs, deflated Sharpe, N_eff, verdict.
arm / disarm     The human permission switch (journaled, --note required).
journal          list / report — the human's measured regime-call accuracy.
status           Overlay state + market regime context (decision support).
paper            Run the live loop against the Binance testnet.
live             Run the live loop against real Binance (guarded prompt).
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from . import __version__, data, indicators
from . import backtest as bt
from .config import Config


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _load_bars(cfg: Config, csv_path: str):
    df_1m = data.load_csv(csv_path)
    return data.aggregate(df_1m, cfg.aggregate_minutes, drop_incomplete=True)


def cmd_test_connection(cfg: Config, args):
    from .broker import Broker

    if not cfg.has_credentials():
        print("No credentials. Set BINANCE_API_KEY and BINANCE_API_SECRET.", file=sys.stderr)
        return 1
    broker = Broker(cfg).connect()
    print(f"Connected. Server time: {broker.server_time()}")
    print(f"Price {cfg.symbol}: {broker.price()}")
    for asset in ("USDT", cfg.symbol.replace("USDT", "")):
        free, locked = broker.balance(asset)
        print(f"{asset}: free={free:.4f} locked={locked:.4f}")
    return 0


def cmd_download(cfg: Config, args):
    from . import download

    symbols = args.symbols or [cfg.symbol]
    start = dt.date.fromisoformat(args.start or cfg.data.start)
    end_str = args.end or cfg.data.end
    end = (dt.date.fromisoformat(end_str) if end_str
           else dt.datetime.now(dt.timezone.utc).date())
    return download.run(symbols, start, end, args.out_dir or cfg.data.dir,
                        source=args.source, allow_gaps=args.allow_gaps,
                        from_listing=args.from_listing)


def cmd_backtest(cfg: Config, args):
    bars = _load_bars(cfg, args.csv)
    frame = indicators.add_all(bars, cfg.strategy)
    result = bt.run(frame, cfg, overlay=cfg.overlay if args.overlay else None)
    print(f"\nBacktest: {cfg.symbol} {cfg.aggregate_minutes}m  ({len(frame)} bars)"
          f"{'  [overlay ON]' if args.overlay else ''}")
    print("-" * 56)
    print(result.summary())
    if args.trades and result.trades:
        print("\nLast trades (newest last):")
        for t in result.trades[-10:]:
            print(f"  {t.reason_out:<11} w={t.weight:.2f} net={t.weighted_net:+.3%} held={t.bars_held}")
    if args.plot:
        from . import report

        bh = frame["close"] / frame["close"].iloc[0]
        out = report.plot_equity(
            result.equity, result.metrics, args.plot,
            title=f"{cfg.symbol} {cfg.aggregate_minutes}m backtest",
            buy_hold=bh.reset_index(drop=True), position=result.position,
        )
        print(f"\nSaved plot to {out}")
    return 0


def cmd_walkforward(cfg: Config, args):
    from . import walkforward as wf

    grid = None
    if args.grid:
        import yaml

        with open(args.grid) as fh:
            grid = yaml.safe_load(fh)

    bars = _load_bars(cfg, args.csv)
    result = wf.run(
        bars, cfg, grid=grid,
        train_bars=args.train_bars, test_bars=args.test_bars,
        step_bars=args.step_bars, min_trades=args.min_trades,
        top_k=args.top_k, embargo_bars=args.embargo,
        overlay=cfg.overlay if args.overlay else None,
    )
    print(f"\nWalk-forward: {cfg.symbol} {cfg.aggregate_minutes}m  ({len(bars)} bars)"
          f"{'  [overlay ON]' if args.overlay else ''}")
    print("-" * 56)
    print(result.summary())
    if args.plot:
        if len(result.oos_returns):
            from . import report

            out = report.plot_equity(
                result.oos_equity().reset_index(drop=True), result.oos_metrics, args.plot,
                title=f"{cfg.symbol} walk-forward (out-of-sample only)",
            )
            print(f"\nSaved plot to {out}")
        else:
            print("\nNo out-of-sample trading; nothing to plot.")
    return 0


def _window_spec(text: str) -> tuple[int, int]:
    """argparse type for ``TRAIN_DAYS:TEST_DAYS`` walk-forward variants."""
    try:
        train, test = (int(x) for x in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected TRAIN_DAYS:TEST_DAYS (e.g. 90:30), got {text!r}") from None
    if train <= 0 or test <= 0:
        raise argparse.ArgumentTypeError("window days must be positive")
    return train, test


def cmd_validate(cfg: Config, args):
    import importlib.util
    import math
    from pathlib import Path

    from . import stats, validate
    from . import walkforward as wf

    v = cfg.validation
    symbols = args.symbols or cfg.data.symbols
    start = args.start or cfg.data.start
    end = args.end if args.end is not None else cfg.data.end
    tag, found, missing = validate.locate_csvs(
        symbols, args.data_dir or cfg.data.dir, start, end)
    if not found:
        print(f"No dated-window CSVs for {', '.join(symbols)} from {start} in "
              f"{args.data_dir or cfg.data.dir}. Run: near-bot download --symbols "
              f"{' '.join(symbols)} --start {start}" + (f" --end {end}" if end else ""),
              file=sys.stderr)
        return 1

    # Both window variants run and both are reported (DESIGN.md §7.1).
    variants = args.windows or list(dict.fromkeys([(v.train_days, v.test_days), (30, 10)]))
    top_k = args.top_k or v.top_k
    n_boot = args.n_boot or v.bootstrap_reps
    block = args.block or v.bootstrap_block_bars
    overlay = cfg.overlay if args.overlay else None
    out_path = Path(args.out)
    plots = not args.no_plots and importlib.util.find_spec("matplotlib") is not None
    bars_per_day = 1440 // cfg.aggregate_minutes
    bars_by_symbol = {s: _load_bars(cfg, str(p)) for s, p in found.items()}

    batteries = []
    for train_d, test_d in variants:
        label = f"train {train_d}d / test {test_d}d"
        print(f"Validating {label} over {', '.join(found)} ...")
        battery = validate.run_battery(
            bars_by_symbol, cfg, train_bars=train_d * bars_per_day,
            test_bars=test_d * bars_per_day, top_k=top_k, n_boot=n_boot,
            block=block, overlay=overlay)
        if plots:
            from . import report

            for sym, res in battery["results"].items():
                if len(res.oos_returns):
                    name = f"validate_{sym}_{train_d}d{test_d}d.png"
                    report.plot_equity(res.oos_equity(), res.oos_metrics,
                                       out_path.parent / name,
                                       title=f"{sym} walk-forward OOS ({label})")
                    battery["symbols"][sym]["plot"] = name
        batteries.append((label, battery))

    p = cfg.strategy
    n_cells = math.prod(len(vals) for vals in wf.DEFAULT_GRID.values())
    header = [
        (f"- Data window: `{tag}` (start inclusive, end exclusive), "
         f"{cfg.aggregate_minutes}m bars aggregated from 1m klines"),
        f"- Symbols: {', '.join(found)}",
        (f"- Engine: walk-forward, Sharpe selection over {n_cells} grid cells, "
         f"top-{top_k} ensemble; risk overlay "
         f"{'ON' if overlay is not None else 'OFF (pure engine)'}"),
        (f"- Costs: taker fee {cfg.costs.taker_fee:.3%} + slippage "
         f"{cfg.costs.slippage:.3%} per side"),
        (f"- Bootstrap: {n_boot:,} circular-block resamples, block {block} bars, "
         f"seed 7, 95% percentile CIs"),
        (f"- Deflated Sharpe: N = trials ledger stats.TRIALS_TRIED = {stats.TRIALS_TRIED}; "
         f"trial-Sharpe variance from the {n_cells} grid cells traded as fixed "
         f"configs on the same OOS bars"),
        (f"- BTC regime: chop if ER({p.er_window}) < {p.er_min}, else up/down vs "
         f"SMA({p.trend_window}); label lagged one bar; descriptive only, never a signal"),
    ]
    if missing:
        header.append("- **Missing data (skipped, not silently): " + ", ".join(
            f"{s} (`{path}`)" for s, path in missing.items()) + "**")
    if not plots:
        header.append("- Plots: skipped" + (" (--no-plots)" if args.no_plots else
                                           " (matplotlib not installed)"))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(validate.render_report(batteries, header, production=cfg.symbol),
                        encoding="utf-8")
    print()
    print("\n".join(validate.verdict_lines(batteries, production=cfg.symbol)))
    if missing:
        print(f"\nMissing data (skipped): {', '.join(missing)}")
    print(f"\nFull report: {out_path}")
    return 0


def cmd_paper(cfg: Config, args):
    from .broker import Broker
    from .executor import Executor

    cfg.paper_trading = True
    if not cfg.overlay.enabled:
        logging.getLogger(__name__).warning(
            "overlay.enabled=false in config — paper/live force it on "
            "(the production posture is what paper trading must rehearse)")
        cfg.overlay.enabled = True
    broker = Broker(cfg).connect()
    Executor(cfg, broker).run(poll_seconds=args.poll)
    return 0


# ------------------------------------------------------------- human overlay

def _default_csv(cfg: Config) -> str:
    """Newest dated-window CSV for the config symbol, or the legacy default."""
    from pathlib import Path

    cands = sorted(Path(cfg.data.dir).glob(f"{cfg.symbol}_1m_*.csv"))
    if cands:
        return str(cands[-1])
    return "data/klines_1m.csv"


def _market_snapshot(cfg: Config) -> tuple[float | str, float | str]:
    """Best-effort (equity, price); blanks when credentials/network are absent."""
    if not cfg.has_credentials():
        return "", ""
    try:
        from .broker import Broker

        broker = Broker(cfg).connect()
        return round(broker.equity_usdt(), 2), broker.price()
    except Exception as exc:  # noqa: BLE001 - a snapshot must never block a decision
        logging.getLogger(__name__).warning("market snapshot failed: %s", exc)
        return "", ""


def _cmd_set_state(cfg: Config, note: str, arm_it: bool) -> int:
    from . import journal
    from . import overlay as ov

    p = cfg.overlay
    state = ov.load_state(p.state_path)
    events = ov.arm(state, p) if arm_it else ov.disarm(state)
    equity, price = _market_snapshot(cfg)
    ov.save_state(state, p.state_path)
    for event, auto_note in events:
        journal.append(p.journal_path, event, state.state, note,
                       equity=equity, price=price, hwm=round(state.hwm, 4),
                       context=auto_note)
    verb = "ARMED" if arm_it else "DISARMED"
    print(f"{verb}. State: {state.state} | cooldown trades left: {state.cooldown_left} | "
          f"journaled to {p.journal_path}")
    if state.cooldown_left and arm_it:
        print(f"Note: first {state.cooldown_left} entries at "
              f"x{p.transition_size_mult} size (transition cooldown).")
    return 0


def cmd_arm(cfg: Config, args):
    return _cmd_set_state(cfg, args.note, arm_it=True)


def cmd_disarm(cfg: Config, args):
    return _cmd_set_state(cfg, args.note, arm_it=False)


def _regime_context(symbol: str) -> str:
    """One line of trend/chop context for a symbol, via public 1h klines.

    Decision *support* for the human's arm/disarm call — never a signal.
    Best-effort: any failure returns an 'unavailable' note.
    """
    import requests

    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 260}, timeout=15)
        resp.raise_for_status()
        df = data.klines_to_df(resp.json())
        close = df["close"]
        ma = indicators.sma(close, 200).iloc[-1]
        er = indicators.efficiency_ratio(close, 48).iloc[-1]
        side = "above" if close.iloc[-1] > ma else "below"
        return f"{symbol}: close {side} 200h MA | ER(48h)={er:.2f}"
    except Exception as exc:  # noqa: BLE001 - decision support is best-effort
        return f"{symbol}: context unavailable ({exc})"


def cmd_status(cfg: Config, args):
    from . import overlay as ov

    p = cfg.overlay
    s = ov.load_state(p.state_path)
    print(f"State          : {s.state.upper()}"
          f"{'  (entries blocked)' if s.state != ov.ARMED else ''}")
    print(f"Size multiplier: x{ov.multiplier(s, p):.2f} "
          f"(tier half={s.half_tier}, cooldown left={s.cooldown_left})")
    print(f"HWM            : {s.hwm:.4f}   halted date: {s.halted_date or '-'}")
    print(f"Day anchor     : {s.day_start_date or '-'} @ {s.day_start_equity:.4f}")
    print(f"Open position  : {s.open_position or '-'}")
    equity, price = _market_snapshot(cfg)
    if equity != "":
        dd = equity / s.hwm - 1.0 if s.hwm else 0.0
        print(f"Equity         : {equity:.2f} USDT ({dd:+.1%} vs HWM) | "
              f"price {cfg.symbol}: {price}")
    print("\nMarket context (decision support, not a signal):")
    for sym in ("BTCUSDT", "ETHUSDT", cfg.symbol):
        print(f"  {_regime_context(sym)}")
    return 0


def cmd_journal(cfg: Config, args):
    from . import journal

    events = journal.load(cfg.overlay.journal_path)
    if args.journal_command == "list":
        if events.empty:
            print("Journal is empty.")
            return 0
        for _, e in events.iterrows():
            print(f"{e['ts_utc']}  {e['event']:<15} {e['state_after']:<9} {e['note']}")
        return 0
    if events.empty:
        print("Journal is empty — no human calls to score yet.")
        return 0
    csv_path = args.csv or _default_csv(cfg)
    bars = _load_bars(cfg, csv_path)
    frame = indicators.add_all(bars, cfg.strategy)
    horizon = args.horizon_days or cfg.validation.journal_horizon_days
    scored = journal.score_calls(events, frame, cfg, horizon_days=horizon)
    rep = journal.accuracy_report(scored, events,
                                  n_boot=cfg.validation.bootstrap_reps)
    print(f"\nJournal report ({cfg.symbol}, forward window {horizon}d, "
          f"data {csv_path})")
    print("-" * 56)
    print(journal.format_report(scored, rep))
    return 0


def cmd_live(cfg: Config, args):
    from .broker import Broker
    from .executor import Executor

    cfg.paper_trading = False
    cfg.overlay.enabled = True  # live always runs with the risk overlay
    confirm = input("Type 'I ACCEPT THE RISK' to trade real funds: ")
    if confirm.strip() != "I ACCEPT THE RISK":
        print("Aborted.")
        return 1
    broker = Broker(cfg).connect()
    Executor(cfg, broker).run(poll_seconds=args.poll)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="near_bot", description="near-momentum-bot v2")
    parser.add_argument("--version", action="version", version=f"near_bot {__version__}")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("test-connection", help="verify API keys")

    d = sub.add_parser("download", help="fetch and save 1m klines over a fixed window")
    d.add_argument("--symbols", nargs="+", default=None,
                   help="default: the config symbol")
    d.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                   help="window start, inclusive (default: data.start from config)")
    d.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                   help="window end, exclusive (default: data.end, or the latest "
                        "complete UTC day)")
    d.add_argument("--out-dir", default=None, help="default: data.dir from config")
    d.add_argument("--allow-gaps", action="store_true",
                   help="accept gaps longer than 1 day instead of failing")
    d.add_argument("--from-listing", action="store_true",
                   help="a symbol listed after --start begins at its first published "
                        "bar (recorded in the manifest) instead of failing")
    d.add_argument("--source", choices=["api", "dumps"], default="dumps",
                   help="'dumps' uses data.binance.vision (no key, no geo-block); "
                        "'api' uses the REST API")

    b = sub.add_parser("backtest", help="backtest over a klines CSV")
    b.add_argument("--csv", default="data/klines_1m.csv")
    b.add_argument("--trades", action="store_true", help="print sample trades")
    b.add_argument("--plot", default=None, metavar="PNG", help="save equity/drawdown plot")
    b.add_argument("--overlay", action="store_true",
                   help="run with the risk overlay (kill switch, R/stop sizing); "
                        "default off keeps the pure engine (JS parity)")

    w = sub.add_parser("walkforward", help="walk-forward validation (out-of-sample report)")
    w.add_argument("--csv", default="data/klines_1m.csv")
    w.add_argument("--train-bars", type=int, default=720, help="in-sample window (720h=30d)")
    w.add_argument("--test-bars", type=int, default=240, help="out-of-sample window (240h=10d)")
    w.add_argument("--step-bars", type=int, default=None, help="window step (default: test-bars)")
    w.add_argument("--min-trades", type=int, default=3,
                   help="min in-sample trades for a combo to be eligible")
    w.add_argument("--top-k", type=int, default=3,
                   help="parameter ensemble size traded out-of-sample")
    w.add_argument("--embargo", type=int, default=0,
                   help="bars skipped between train and test windows")
    w.add_argument("--grid", default=None, metavar="YAML",
                   help="YAML mapping of strategy param -> list of values")
    w.add_argument("--plot", default=None, metavar="PNG", help="save OOS equity plot")
    w.add_argument("--overlay", action="store_true",
                   help="validate with the risk overlay active (the production posture)")

    val = sub.add_parser("validate", help="validation battery: multi-symbol walk-forward, "
                                          "bootstrap CIs, deflated Sharpe, N_eff, verdict")
    val.add_argument("--symbols", nargs="+", default=None,
                     help="default: data.symbols from config (BTC = context stream)")
    val.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                     help="window start, inclusive (default: data.start)")
    val.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                     help="window end, exclusive (default: data.end, or the newest "
                          "downloaded window from --start)")
    val.add_argument("--data-dir", default=None, help="default: data.dir from config")
    val.add_argument("--windows", nargs="+", type=_window_spec, default=None,
                     metavar="TRAIN:TEST",
                     help="walk-forward variants in days (default: validation."
                          "train_days:test_days and 30:10 -- both run, both reported)")
    val.add_argument("--top-k", type=int, default=None,
                     help="ensemble size (default: validation.top_k)")
    val.add_argument("--n-boot", type=int, default=None,
                     help="bootstrap resamples (default: validation.bootstrap_reps)")
    val.add_argument("--block", type=int, default=None,
                     help="bootstrap block length in bars "
                          "(default: validation.bootstrap_block_bars)")
    val.add_argument("--overlay", action="store_true",
                     help="validate with the risk overlay active (default off, "
                          "as for backtest/walkforward)")
    val.add_argument("--out", default="reports/validate.md",
                     help="markdown report path; plots are written next to it")
    val.add_argument("--no-plots", action="store_true", help="skip the equity plots")

    p = sub.add_parser("paper", help="run on testnet")
    p.add_argument("--poll", type=int, default=30)

    live = sub.add_parser("live", help="run on real Binance (guarded)")
    live.add_argument("--poll", type=int, default=30)

    a = sub.add_parser("arm", help="permit new entries (regime call: ON)")
    a.add_argument("--note", required=True,
                   help="your reason, in your words — journaled and scored later")

    d2 = sub.add_parser("disarm", help="block new entries (regime call: OFF; "
                                       "open positions keep their engine exits)")
    d2.add_argument("--note", required=True,
                    help="your reason, in your words — journaled and scored later")

    j = sub.add_parser("journal", help="the human decision journal")
    jsub = j.add_subparsers(dest="journal_command", required=True)
    jsub.add_parser("list", help="print every journaled event")
    jr = jsub.add_parser("report", help="score arm/disarm calls against outcomes")
    jr.add_argument("--csv", default=None,
                    help="1m klines CSV for the replay (default: newest dated "
                         "window for the config symbol)")
    jr.add_argument("--horizon-days", type=int, default=None,
                    help="forward scoring window (default: validation.journal_horizon_days)")

    sub.add_parser("status", help="overlay state + market regime context")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    cfg = Config.load(args.config)

    dispatch = {
        "test-connection": cmd_test_connection,
        "download": cmd_download,
        "backtest": cmd_backtest,
        "walkforward": cmd_walkforward,
        "validate": cmd_validate,
        "paper": cmd_paper,
        "live": cmd_live,
        "arm": cmd_arm,
        "disarm": cmd_disarm,
        "journal": cmd_journal,
        "status": cmd_status,
    }
    return dispatch[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())

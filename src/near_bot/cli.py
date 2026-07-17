"""Command-line interface: ``near-bot <command>`` or ``python -m near_bot ...``.

Subcommands
-----------
test-connection  Verify API keys and print account / symbol info.
download         Fetch 1m klines to CSV (REST API or data.binance.vision dumps).
backtest         Run the strategy over a CSV of 1-minute klines.
walkforward      Optimise on rolling in-sample windows, report out-of-sample.
paper            Run the live loop against the Binance testnet.
live             Run the live loop against real Binance (guarded prompt).
"""

from __future__ import annotations

import argparse
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
    if args.source == "dumps":
        from . import dump_client

        df = dump_client.fetch_daily(cfg.symbol, args.days)
    else:
        from .broker import Broker

        broker = Broker(cfg).connect()
        klines = broker.get_klines_history(cfg.base_interval, args.days * 1440)
        df = data.klines_to_df(klines)
    data.save_csv(df, args.out)
    print(f"Saved {len(df)} 1m klines to {args.out}")
    return 0


def cmd_backtest(cfg: Config, args):
    bars = _load_bars(cfg, args.csv)
    frame = indicators.add_all(bars, cfg.strategy)
    result = bt.run(frame, cfg)
    print(f"\nBacktest: {cfg.symbol} {cfg.aggregate_minutes}m  ({len(frame)} bars)")
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
    )
    print(f"\nWalk-forward: {cfg.symbol} {cfg.aggregate_minutes}m  ({len(bars)} bars)")
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


def cmd_paper(cfg: Config, args):
    from .broker import Broker
    from .executor import Executor

    cfg.paper_trading = True
    broker = Broker(cfg).connect()
    Executor(cfg, broker).run(poll_seconds=args.poll)
    return 0


def cmd_live(cfg: Config, args):
    from .broker import Broker
    from .executor import Executor

    cfg.paper_trading = False
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

    d = sub.add_parser("download", help="fetch and save 1m klines")
    d.add_argument("--days", type=int, default=30)
    d.add_argument("--out", default="data/klines_1m.csv")
    d.add_argument("--source", choices=["api", "dumps"], default="dumps",
                   help="'dumps' uses data.binance.vision (no key, no geo-block); "
                        "'api' uses the REST API")

    b = sub.add_parser("backtest", help="backtest over a klines CSV")
    b.add_argument("--csv", default="data/klines_1m.csv")
    b.add_argument("--trades", action="store_true", help="print sample trades")
    b.add_argument("--plot", default=None, metavar="PNG", help="save equity/drawdown plot")

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

    p = sub.add_parser("paper", help="run on testnet")
    p.add_argument("--poll", type=int, default=30)

    live = sub.add_parser("live", help="run on real Binance (guarded)")
    live.add_argument("--poll", type=int, default=30)
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
        "paper": cmd_paper,
        "live": cmd_live,
    }
    return dispatch[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())

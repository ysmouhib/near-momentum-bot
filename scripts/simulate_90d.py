#!/usr/bin/env python3
"""One command: pull ~90 days of real 1m data and simulate the bot.

Runs both the fixed-config backtest and the walk-forward (out-of-sample)
validation, then prints a plain gain/loss summary. No API key needed — uses
Binance's public data dumps (data.binance.vision), which also works from
regions where the REST API is blocked.

    python scripts/simulate_90d.py --symbol NEARUSDT --days 90

Outputs plots to reports/ when matplotlib is installed (pip install -e ".[plot]").
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from near_bot import backtest as bt  # noqa: E402
from near_bot import data, indicators  # noqa: E402
from near_bot import walkforward as wf  # noqa: E402
from near_bot.config import Config  # noqa: E402
from near_bot.dump_client import fetch_daily  # noqa: E402


def _usd(pct: float, capital: float) -> str:
    return f"{pct:+.2%}  ({capital * pct:+,.2f} USDT on {capital:,.0f})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NEARUSDT")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--capital", type=float, default=1000.0,
                    help="starting capital, for the USDT gain/loss column")
    ap.add_argument("--plot", action="store_true")
    args = ap.parse_args(argv)

    cfg = Config()
    cfg.symbol = args.symbol
    df_1m = fetch_daily(args.symbol, args.days)
    Path("data").mkdir(exist_ok=True)
    data.save_csv(df_1m, "data/klines_1m.csv")

    bars = data.aggregate(df_1m, cfg.aggregate_minutes, drop_incomplete=True)
    frame = indicators.add_all(bars, cfg.strategy)

    bt_res = bt.run(frame, cfg)
    try:
        wf_res = wf.run(bars, cfg)
    except ValueError as exc:
        print(f"\nWalk-forward skipped: {exc}")
        print(f"Fetch more days (current: {args.days}) or pass shorter windows.")
        wf_res = wf.WalkForwardResult()

    bm, wm = bt_res.metrics, wf_res.oos_metrics
    print("\n" + "=" * 62)
    print(f"{args.symbol}  {cfg.aggregate_minutes}m  |  {len(bars):,} bars  |  ~{args.days} days")
    print("=" * 62)
    print("\nFIXED-CONFIG BACKTEST (optimistic — parameters fixed over the whole window)")
    if bm.get("n_trades", 0):
        print(f"  trades          : {bm['n_trades']}")
        print(f"  win rate        : {bm['win_rate']:.1%}")
        print(f"  expectancy/trade: {bm['expectancy_weighted']:+.3%} net (drag {bm['cost_drag']:.3%})")
        print(f"  total return    : {_usd(bm['total_return'], args.capital)}")
        print(f"  sharpe (ann.)   : {bm['sharpe']:+.2f}")
        print(f"  max drawdown    : {bm['max_drawdown']:.2%}")
        print(f"  exposure        : {bm['exposure']:.1%}")
    else:
        print("  no trades taken")

    print("\nWALK-FORWARD OUT-OF-SAMPLE (the number that actually matters)")
    if wm.get("n_trades", 0):
        print(f"  OOS trades      : {wm['n_trades']}")
        print(f"  OOS win rate    : {wm['win_rate']:.1%}")
        print(f"  OOS expectancy  : {wm['expectancy_weighted']:+.3%} net (drag {wm['cost_drag']:.3%})")
        print(f"  OOS total return: {_usd(wm['total_return'], args.capital)}")
        print(f"  OOS buy & hold  : {_usd(wm['buy_hold'], args.capital)}")
        print(f"  OOS sharpe      : {wm['sharpe']:+.2f}")
        print(f"  OOS max drawdown: {wm['max_drawdown']:.2%}")
    else:
        print("  no out-of-sample trades (all windows skipped or no signals)")
    print("\nReminder: simulated results ignore partial fills, depth and latency,")
    print("and are an upper bound — not a promise. Not financial advice.\n")

    if args.plot:
        from near_bot import report
        bh = frame["close"] / frame["close"].iloc[0]
        report.plot_equity(bt_res.equity, bm, "reports/backtest_90d.png",
                           title=f"{args.symbol} {cfg.aggregate_minutes}m backtest ({args.days}d)",
                           buy_hold=bh.reset_index(drop=True), position=bt_res.position)
        if len(wf_res.oos_returns):
            report.plot_equity(wf_res.oos_equity().reset_index(drop=True), wm,
                               "reports/walkforward_90d.png",
                               title=f"{args.symbol} walk-forward OOS ({args.days}d)")
        print("Saved plots to reports/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

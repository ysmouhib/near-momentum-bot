"""Thin wrapper around python-binance for spot trading.

Scope is deliberately small: connect (testnet or live), read balances and
klines (with pagination, since the v2 warmup needs far more than the
1000-bar single-request limit), and place market orders with correct
lot-size rounding and a min-notional check. Long-only — there is no short
path on spot.

python-binance is imported lazily so the backtester and unit tests run
without it (and without any network access).
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

logger = logging.getLogger(__name__)


class Broker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.symbol = cfg.symbol
        self._client = None
        self.min_qty = None
        self.step_size = None
        self.min_notional = None

    # -- connection -------------------------------------------------------
    def connect(self):
        from binance.client import Client  # lazy import

        if self.cfg.paper_trading:
            logger.info("Connecting to Binance TESTNET (paper trading)")
            client = Client(self.cfg.api_key, self.cfg.api_secret, testnet=True)
            # testnet=True already sets the testnet base; only override when
            # the config points elsewhere, keeping the /api path.
            client.API_URL = self.cfg.testnet_url.rstrip("/") + "/api"
        else:
            logger.warning("Connecting to Binance LIVE -- real funds at risk")
            client = Client(self.cfg.api_key, self.cfg.api_secret)
        self._client = client
        self._load_symbol_filters()
        return self

    @property
    def client(self):
        if self._client is None:
            raise RuntimeError("Broker.connect() must be called first")
        return self._client

    def _load_symbol_filters(self):
        info = self.client.get_symbol_info(self.symbol)
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                self.min_qty = float(f["minQty"])
                self.step_size = float(f["stepSize"])
            elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                self.min_notional = float(f.get("minNotional", f.get("notional", 0)))
        logger.info(
            "Filters: min_qty=%s step=%s min_notional=%s",
            self.min_qty, self.step_size, self.min_notional,
        )

    # -- reads ------------------------------------------------------------
    def server_time(self) -> int:
        return self.client.get_server_time()["serverTime"]

    def price(self) -> float:
        return float(self.client.get_symbol_ticker(symbol=self.symbol)["price"])

    def balance(self, asset: str) -> tuple[float, float]:
        for b in self.client.get_account()["balances"]:
            if b["asset"] == asset:
                return float(b["free"]), float(b["locked"])
        return 0.0, 0.0

    def get_klines(self, interval: str, limit: int = 200) -> list:
        return self.client.get_klines(symbol=self.symbol, interval=interval, limit=limit)

    def get_klines_history(self, interval: str, n_bars: int) -> list:
        """Fetch the last ``n_bars`` klines, paginating the 1000/request limit."""
        out: list = []
        end = self.server_time()
        remaining = n_bars
        while remaining > 0:
            batch = self.client.get_klines(
                symbol=self.symbol, interval=interval,
                limit=min(1000, remaining), endTime=end,
            )
            if not batch:
                break
            out = batch + out
            end = batch[0][0] - 1
            remaining -= len(batch)
            if len(batch) < min(1000, remaining + len(batch)):
                break  # exchange returned fewer than asked: no older data
            time.sleep(0.2)  # be polite
        return out

    # -- orders (long-only) ----------------------------------------------
    def round_qty(self, qty: float) -> float:
        """Round *down* to the symbol's LOT_SIZE step (Decimal: float modulo
        breaks on step sizes like 1e-05)."""
        if not self.step_size:
            return qty
        step = Decimal(str(self.step_size))
        stepped = (Decimal(str(qty)) // step) * step
        return float(stepped)

    def _market_order(self, side: str, qty: float, price_hint: float | None):
        from binance.enums import ORDER_TYPE_MARKET
        from binance.exceptions import BinanceAPIException

        qty = self.round_qty(qty)
        if self.min_qty and qty < self.min_qty:
            logger.error("Qty %s below min_qty %s -- skipping", qty, self.min_qty)
            return None
        if self.min_notional and price_hint and qty * price_hint < self.min_notional:
            logger.error(
                "Notional %.2f below min_notional %.2f -- skipping",
                qty * price_hint, self.min_notional,
            )
            return None
        try:
            order = self.client.create_order(
                symbol=self.symbol, side=side, type=ORDER_TYPE_MARKET, quantity=qty,
            )
            logger.info("Order %s %s -> id=%s status=%s",
                        side, qty, order["orderId"], order["status"])
            return order
        except BinanceAPIException as exc:
            logger.error("Binance API error placing %s: %s", side, exc)
            return None

    def buy(self, qty: float, price_hint: float | None = None):
        from binance.enums import SIDE_BUY

        return self._market_order(SIDE_BUY, qty, price_hint)

    def sell(self, qty: float, price_hint: float | None = None):
        from binance.enums import SIDE_SELL

        return self._market_order(SIDE_SELL, qty, price_hint)

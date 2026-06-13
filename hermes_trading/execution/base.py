"""Abstract Executor interface — paper and live adapters both implement this."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Order:
    asset: str            # e.g. "BTC/USDT" (canonical, ccxt-style)
    side: str             # "buy" or "sell"
    qty: float            # quantity in base currency
    filled_price: float   # actual fill price
    timestamp: str        # ISO8601 UTC
    order_id: str         # exchange-assigned id (or synthetic for paper)
    status: str = "filled"  # "filled" | "rejected" | "partial"


@dataclass
class Position:
    asset: str
    qty: float            # signed: positive = long, negative = short
    avg_entry_price: float
    market_value: float   # qty * current_price
    unrealized_pnl_pct: float


class Executor(ABC):
    """Interface every execution adapter implements."""

    @abstractmethod
    async def fetch_cash(self) -> float:
        """Available cash for new positions."""

    @abstractmethod
    async def fetch_position(self, asset: str) -> Optional[Position]:
        """Current open position for the asset, or None."""

    @abstractmethod
    async def place_market_order(
        self,
        asset: str,
        side: str,
        position_size_r: float,
        current_price: float,
        tradable_cash: float,
    ) -> Order:
        """
        Place a market order. Deploys `tradable_cash * position_size_r` dollars.
        `tradable_cash` is computed by the caller (cash minus vault/fund claims).
        Adapters enforce the MAX_POSITION_PCT kill switch before submitting.
        """

    @abstractmethod
    async def close_position(self, asset: str, current_price: float) -> Optional[Order]:
        """Close the open position for `asset`. Returns the exit Order, or None if no position."""

    @abstractmethod
    async def place_stock_buy(self, ticker: str, dollars: float) -> Order:
        """
        Buy `dollars` worth of `ticker` at market — fractional shares OK.
        Used by the treasury for DCA and vault parking. Distinct from
        place_market_order (which is for the trading strategy's main asset).
        """

    async def count_open_orders(self, asset: str) -> int:
        """Number of unfilled/pending orders for `asset`. Default 0 (paper).
        Live adapters override this — used to prevent runaway order stacking."""
        return 0

    async def cancel_open_orders(self, asset: str) -> int:
        """Cancel pending orders for `asset`; return count cancelled. Default 0 (paper)."""
        return 0

    @property
    @abstractmethod
    def mode(self) -> str:
        """`paper` or `live`. Used in logging and trade records."""

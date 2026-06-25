"""Paper executor — simulates fills in memory, tracks PnL against price data."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Optional

from .base import Executor, Order, Position


class PaperExecutor(Executor):
    def __init__(self, starting_cash: float = 10000.0):
        self._cash = starting_cash
        self._positions: dict[str, Position] = {}

    @property
    def mode(self) -> str:
        return "paper"

    async def fetch_cash(self) -> float:
        return self._cash

    async def fetch_position(self, asset: str) -> Optional[Position]:
        return self._positions.get(asset)

    async def fetch_all_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def place_market_order(
        self,
        asset: str,
        side: str,
        position_size_r: float,
        current_price: float,
        tradable_cash: float,
    ) -> Order:
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        dollars_to_deploy = tradable_cash * position_size_r
        qty = dollars_to_deploy / current_price
        signed_qty = qty if side == "buy" else -qty

        # Lock the cash into the position
        self._cash -= dollars_to_deploy
        self._positions[asset] = Position(
            asset=asset,
            qty=signed_qty,
            avg_entry_price=current_price,
            market_value=dollars_to_deploy,
            unrealized_pnl_pct=0.0,
        )

        return Order(
            asset=asset,
            side=side,
            qty=qty,
            filled_price=current_price,
            timestamp=datetime.now(timezone.utc).isoformat(),
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
        )

    async def close_position(self, asset: str, current_price: float) -> Optional[Order]:
        pos = self._positions.get(asset)
        if pos is None:
            return None

        # Realize the PnL back into cash
        exit_value = abs(pos.qty) * current_price
        entry_value = abs(pos.qty) * pos.avg_entry_price
        pnl = (exit_value - entry_value) if pos.qty > 0 else (entry_value - exit_value)
        self._cash += entry_value + pnl

        side = "sell" if pos.qty > 0 else "buy"
        order = Order(
            asset=asset,
            side=side,
            qty=abs(pos.qty),
            filled_price=current_price,
            timestamp=datetime.now(timezone.utc).isoformat(),
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
        )
        del self._positions[asset]
        return order

    async def place_stock_buy(self, ticker: str, dollars: float) -> Order:
        """Paper executor doesn't model stock holdings — log a synthetic order."""
        if dollars > self._cash:
            raise RuntimeError(f"Paper executor: cannot stock-buy ${dollars:.2f}, only ${self._cash:.2f} cash")
        self._cash -= dollars
        return Order(
            asset=ticker,
            side="buy",
            qty=0.0,  # unknown without price; this is a logical accounting entry
            filled_price=0.0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
        )

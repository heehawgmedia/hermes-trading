"""Execution layer — abstracts paper vs live trading behind one interface."""
from __future__ import annotations
import os

from .base import Executor, Order, Position
from .paper import PaperExecutor
from .alpaca import AlpacaExecutor


def get_executor() -> Executor:
    """Factory — returns the right executor based on HERMES_TRADING_MODE."""
    mode = os.getenv("HERMES_TRADING_MODE", "paper").lower()

    if mode == "paper":
        return PaperExecutor(starting_cash=float(os.getenv("PAPER_STARTING_CASH", "10000")))

    if mode == "live":
        if os.getenv("HERMES_TRADING_I_ACCEPT_RISK", "").lower() != "true":
            raise RuntimeError(
                "Live mode requires HERMES_TRADING_I_ACCEPT_RISK=true in .env"
            )
        key = os.getenv("ALPACA_API_KEY", "").strip()
        secret = os.getenv("ALPACA_API_SECRET", "").strip()
        if not key or not secret:
            raise RuntimeError(
                "Live mode requires ALPACA_API_KEY and ALPACA_API_SECRET in .env"
            )
        # ALPACA_BASE_URL switches between paper (default) and live:
        #   paper: https://paper-api.alpaca.markets   ← safe test endpoint
        #   live:  https://api.alpaca.markets         ← real money
        base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        max_position_pct = float(os.getenv("MAX_POSITION_PCT", "0.02"))  # 2% kill switch
        return AlpacaExecutor(
            api_key=key,
            api_secret=secret,
            base_url=base_url,
            max_position_pct=max_position_pct,
        )

    raise ValueError(f"Unknown HERMES_TRADING_MODE: {mode!r}")


__all__ = ["Executor", "Order", "Position", "get_executor"]

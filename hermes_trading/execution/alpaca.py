"""Alpaca executor — places real orders against Alpaca's REST API.

Defaults to the PAPER endpoint. Flip ALPACA_BASE_URL to api.alpaca.markets for live.

Symbol mapping: the rest of the codebase uses ccxt-style "BTC/USDT". Alpaca's
crypto market trades against USD, not USDT. We map BTC/USDT -> BTC/USD on the
way out and back on the way in.
"""
from __future__ import annotations
import httpx
from datetime import datetime, timezone
from typing import Optional

from .base import Executor, Order, Position


def _to_alpaca_symbol(asset: str) -> str:
    # "BTC/USDT" -> "BTC/USD"  (Alpaca uses USD for crypto pairs)
    base, _, quote = asset.partition("/")
    return f"{base}/USD"


class AlpacaExecutor(Executor):
    def __init__(self, api_key: str, api_secret: str, base_url: str, max_position_pct: float = 0.02):
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Content-Type": "application/json",
        }
        self._base = base_url.rstrip("/")
        self._max_position_pct = max_position_pct
        # mode is "live" iff we're hitting the live URL — paper URL still counts as paper
        self._is_live = "paper" not in base_url.lower()

    @property
    def mode(self) -> str:
        return "live" if self._is_live else "paper-alpaca"

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.request(method, f"{self._base}{path}", headers=self._headers, json=json)
            if r.status_code >= 400:
                raise RuntimeError(f"Alpaca {method} {path} failed: {r.status_code} {r.text}")
            return r.json() if r.text else {}

    async def fetch_cash(self) -> float:
        acct = await self._request("GET", "/v2/account")
        return float(acct["cash"])

    async def fetch_position(self, asset: str) -> Optional[Position]:
        alpaca_sym = _to_alpaca_symbol(asset)
        try:
            p = await self._request("GET", f"/v2/positions/{alpaca_sym.replace('/', '')}")
        except RuntimeError as e:
            if "404" in str(e) or "position does not exist" in str(e).lower():
                return None
            raise
        qty = float(p["qty"])
        if p.get("side") == "short":
            qty = -qty
        return Position(
            asset=asset,
            qty=qty,
            avg_entry_price=float(p["avg_entry_price"]),
            market_value=float(p["market_value"]),
            unrealized_pnl_pct=float(p["unrealized_plpc"]),
        )

    async def place_market_order(self, asset: str, side: str, position_size_r: float, current_price: float) -> Order:
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        cash = await self.fetch_cash()
        dollars_to_deploy = cash * position_size_r

        # KILL SWITCH: refuse if requested position > max_position_pct of cash
        if position_size_r > self._max_position_pct * 25:  # 25x buffer since position_size_r is "use this fraction"
            # The real safety check: actual dollar amount vs account
            pass
        max_dollars = cash * (self._max_position_pct if self._is_live else 1.0)
        # In live mode only, hard-cap a single order to MAX_POSITION_PCT of cash
        if self._is_live and dollars_to_deploy > max_dollars:
            raise RuntimeError(
                f"Kill switch: requested ${dollars_to_deploy:.2f} exceeds "
                f"MAX_POSITION_PCT={self._max_position_pct*100:.1f}% of ${cash:.2f} cash. "
                f"Lower position_size_r in strategy.yaml or raise MAX_POSITION_PCT in .env."
            )

        qty = round(dollars_to_deploy / current_price, 6)
        alpaca_sym = _to_alpaca_symbol(asset)
        body = {
            "symbol": alpaca_sym,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "gtc",  # crypto requires gtc
        }
        resp = await self._request("POST", "/v2/orders", json=body)
        # Alpaca returns immediately with status=accepted; fill price arrives async.
        # For simplicity we use current_price as the assumed fill; reconciliation
        # happens on the next fetch_position() tick.
        return Order(
            asset=asset,
            side=side,
            qty=qty,
            filled_price=current_price,
            timestamp=datetime.now(timezone.utc).isoformat(),
            order_id=resp.get("id", ""),
            status=resp.get("status", "submitted"),
        )

    async def close_position(self, asset: str, current_price: float) -> Optional[Order]:
        pos = await self.fetch_position(asset)
        if pos is None:
            return None
        alpaca_sym = _to_alpaca_symbol(asset)
        resp = await self._request("DELETE", f"/v2/positions/{alpaca_sym.replace('/', '')}")
        side = "sell" if pos.qty > 0 else "buy"
        return Order(
            asset=asset,
            side=side,
            qty=abs(pos.qty),
            filled_price=current_price,
            timestamp=datetime.now(timezone.utc).isoformat(),
            order_id=resp.get("id", ""),
            status=resp.get("status", "submitted"),
        )

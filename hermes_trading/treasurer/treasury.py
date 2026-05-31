"""Treasury — skims winning trades into a vault and a stock-purchase fund.

On every profitable exit:
  - VAULT_SKIM_PCT of the profit → vault (held as cash, optionally parked in a yield ticker)
  - STOCK_FUND_SKIM_PCT of the profit → stock fund (accumulates until ≥ STOCK_FUND_DCA_THRESHOLD,
    then converts into a buy of DCA_TICKER)

The remaining profit stays as tradable cash for the bot.

Both pools are logical balances on top of the Alpaca cash. The trading loop
subtracts them when computing what cash it can deploy.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TREASURY_PATH = Path("state/treasury.json")


@dataclass
class TreasuryConfig:
    vault_skim_pct: float = 0.10           # 10% of each profitable trade
    stock_fund_skim_pct: float = 0.20      # 20% of each profitable trade
    stock_fund_dca_threshold: float = 25.0 # buy stocks once this many $ pile up
    dca_ticker: str = ""                   # "" = accumulate but don't buy yet
    vault_park_ticker: str = ""            # "" = hold as cash; "BIL" = park in T-bills

    @classmethod
    def from_env(cls) -> "TreasuryConfig":
        return cls(
            vault_skim_pct=float(os.getenv("VAULT_SKIM_PCT", "0.10")),
            stock_fund_skim_pct=float(os.getenv("STOCK_FUND_SKIM_PCT", "0.20")),
            stock_fund_dca_threshold=float(os.getenv("STOCK_FUND_DCA_THRESHOLD", "25")),
            dca_ticker=os.getenv("DCA_TICKER", "").strip().upper(),
            vault_park_ticker=os.getenv("VAULT_PARK_TICKER", "").strip().upper(),
        )


def _empty_state() -> dict[str, Any]:
    return {
        "vault_balance": 0.0,
        "vault_deposits": [],
        "vault_park_history": [],
        "stock_fund_balance": 0.0,
        "stock_fund_deposits": [],
        "dca_history": [],
    }


class Treasury:
    def __init__(self, config: TreasuryConfig | None = None):
        self.config = config or TreasuryConfig.from_env()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if TREASURY_PATH.exists():
            try:
                return json.loads(TREASURY_PATH.read_text())
            except Exception:
                return _empty_state()
        return _empty_state()

    def _save(self) -> None:
        TREASURY_PATH.write_text(json.dumps(self.state, indent=2))

    @property
    def vault_balance(self) -> float:
        return self.state["vault_balance"]

    @property
    def stock_fund_balance(self) -> float:
        return self.state["stock_fund_balance"]

    @property
    def claimed_cash(self) -> float:
        """Total cash that's logically reserved — bot must subtract this from tradable cash."""
        return self.vault_balance + self.stock_fund_balance

    async def on_winning_trade(self, profit_dollars: float, executor, source_trade_id: str = "") -> dict:
        """Skim from a winning trade. Returns summary of what happened."""
        if profit_dollars <= 0:
            return {"skipped": "not a winning trade"}

        now = datetime.now(timezone.utc).isoformat()
        vault_skim = round(profit_dollars * self.config.vault_skim_pct, 4)
        fund_skim = round(profit_dollars * self.config.stock_fund_skim_pct, 4)

        # Deposit into vault
        self.state["vault_balance"] = round(self.state["vault_balance"] + vault_skim, 4)
        self.state["vault_deposits"].append({
            "t": now, "amount": vault_skim, "source_trade": source_trade_id,
        })

        # Deposit into stock fund
        self.state["stock_fund_balance"] = round(self.state["stock_fund_balance"] + fund_skim, 4)
        self.state["stock_fund_deposits"].append({
            "t": now, "amount": fund_skim, "source_trade": source_trade_id,
        })

        summary = {
            "profit": profit_dollars,
            "vault_skim": vault_skim,
            "fund_skim": fund_skim,
            "vault_balance": self.state["vault_balance"],
            "stock_fund_balance": self.state["stock_fund_balance"],
            "dca_fired": False,
            "vault_parked": False,
        }

        # Maybe park the vault deposit into a yield ticker (e.g. BIL)
        if self.config.vault_park_ticker and vault_skim > 0:
            try:
                order = await executor.place_stock_buy(self.config.vault_park_ticker, vault_skim)
                self.state["vault_park_history"].append({
                    "t": now, "ticker": self.config.vault_park_ticker,
                    "dollars": vault_skim, "order_id": order.order_id,
                })
                summary["vault_parked"] = True
                print(f"[VAULT] parked ${vault_skim:.2f} into {self.config.vault_park_ticker}", flush=True)
            except Exception as e:
                print(f"[VAULT] park failed (will retry next time): {e}", flush=True)

        # Maybe fire DCA buy if fund crossed threshold
        if (self.state["stock_fund_balance"] >= self.config.stock_fund_dca_threshold
                and self.config.dca_ticker):
            dca_dollars = self.state["stock_fund_balance"]
            try:
                order = await executor.place_stock_buy(self.config.dca_ticker, dca_dollars)
                self.state["dca_history"].append({
                    "t": now, "ticker": self.config.dca_ticker,
                    "dollars": dca_dollars, "order_id": order.order_id,
                })
                self.state["stock_fund_balance"] = 0.0
                summary["dca_fired"] = True
                summary["dca_dollars"] = dca_dollars
                print(f"[DCA] bought ${dca_dollars:.2f} of {self.config.dca_ticker}", flush=True)
            except Exception as e:
                print(f"[DCA] buy failed (will retry next time): {e}", flush=True)
        elif (self.state["stock_fund_balance"] >= self.config.stock_fund_dca_threshold
                and not self.config.dca_ticker):
            # Fund is ready but user hasn't picked a ticker — print a reminder.
            print(
                f"[DCA] fund at ${self.state['stock_fund_balance']:.2f} (≥ threshold), "
                f"but DCA_TICKER not set. Set it on Railway to start buying.",
                flush=True,
            )

        self._save()
        return summary

"""24/7 reliability loop — pulls data, evaluates strategy, delegates execution."""
from __future__ import annotations
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from hermes_trading.adapters import price as price_adapter
from hermes_trading.execution import get_executor
from hermes_trading.treasurer import Treasury

TRADES_PATH = Path("state/trades.jsonl")
STRATEGY_PATH = Path("state/strategy.yaml")
GOAL_PATH = Path("state/goal.yaml")
HEARTBEAT_PATH = Path("state/heartbeat.json")

MAX_CONSECUTIVE_FAILURES = 5
RETRY_ATTEMPTS = 3
LOOP_INTERVAL_SECONDS = 60


async def _fetch_with_retry(adapter, *args, **kwargs):
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return await adapter.fetch(*args, **kwargs)
        except Exception:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            await asyncio.sleep(2 ** attempt)


def _load_strategy() -> dict:
    with open(STRATEGY_PATH) as f:
        return yaml.safe_load(f)


def _compute_rsi(prices: list[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    import numpy as np
    deltas = np.diff(prices[-period - 1:])
    gains = deltas[deltas > 0].mean() if any(d > 0 for d in deltas) else 0.0
    losses = -deltas[deltas < 0].mean() if any(d < 0 for d in deltas) else 1e-9
    rs = gains / losses if losses else 0
    return 100 - (100 / (1 + rs))


_price_history: list[float] = []


def _evaluate_strategy(strategy: dict, market: dict, has_position: bool, entry_price: float | None) -> str:
    """Returns 'enter', 'exit', or 'hold'."""
    price = market["price"]
    entry_cfg = strategy.get("entry", {})
    indicator = entry_cfg.get("indicator", "rsi")
    threshold = float(entry_cfg.get("threshold", 30))
    direction = entry_cfg.get("direction", "long")
    stop_loss_pct = float(strategy.get("stop_loss_pct", 2.0)) / 100
    take_profit_pct = float(strategy.get("take_profit_pct", 0)) / 100  # 0 = disabled

    if indicator == "rsi":
        rsi = _compute_rsi(_price_history)
        if not has_position:
            if direction == "long" and rsi < threshold:
                return "enter"
        else:
            assert entry_price is not None
            if direction == "long" and price < entry_price * (1 - stop_loss_pct):
                return "exit"  # stop loss
            # Price-based take-profit: bank the bounce as a win before the stop fires.
            if take_profit_pct > 0 and direction == "long" and price >= entry_price * (1 + take_profit_pct):
                return "exit"  # take profit (price target hit)
            if direction == "long" and rsi > 70:
                return "exit"  # take profit (momentum exhausted)
    return "hold"


def _log_trade(trade: dict) -> None:
    with open(TRADES_PATH, "a") as f:
        f.write(json.dumps(trade) + "\n")


def _write_heartbeat(status: str, consecutive_failures: int, extra: dict | None = None) -> None:
    data = {
        "status": status,
        "last_tick": datetime.now(timezone.utc).isoformat(),
        "consecutive_failures": consecutive_failures,
    }
    if extra:
        data.update(extra)
    with open(HEARTBEAT_PATH, "w") as f:
        json.dump(data, f)


async def run_loop(asset: str) -> None:
    global _price_history
    consecutive_failures = 0
    executor = get_executor()
    treasury = Treasury()
    print(
        f"Booting hermes-trading worker — asset={asset} mode={executor.mode} "
        f"vault_skim={treasury.config.vault_skim_pct*100:.0f}% "
        f"fund_skim={treasury.config.stock_fund_skim_pct*100:.0f}% "
        f"dca_threshold=${treasury.config.stock_fund_dca_threshold:.0f} "
        f"dca_ticker={treasury.config.dca_ticker or '(unset)'}",
        flush=True,
    )

    while True:
        tick_start = time.monotonic()
        try:
            market = await _fetch_with_retry(price_adapter, asset)
            price = market["price"]
            _price_history.append(price)
            if len(_price_history) > 200:
                _price_history = _price_history[-200:]

            strategy = _load_strategy()
            position = await executor.fetch_position(asset)
            decision = _evaluate_strategy(
                strategy, market,
                has_position=position is not None,
                entry_price=position.avg_entry_price if position else None,
            )

            # --- Order actions are RECOVERABLE: a rejected/failed order logs and
            # --- continues. It must NOT trip the connectivity circuit breaker,
            # --- otherwise one Alpaca business-rule rejection crashes the worker.
            try:
                if decision == "enter":
                    position_size_r = float(strategy.get("position_size_r", 0.5))
                    direction = strategy.get("entry", {}).get("direction", "long")
                    side = "buy" if direction == "long" else "sell"
                    # Tradable cash = total cash minus vault & stock fund claims
                    tradable_cash = max(0.0, (await executor.fetch_cash()) - treasury.claimed_cash)
                    order = await executor.place_market_order(
                        asset, side,
                        position_size_r=position_size_r,
                        current_price=price,
                        tradable_cash=tradable_cash,
                    )
                    print(f"[ENTER] {executor.mode} {asset} {side} qty={order.qty:.6f} @ {price:.2f} "
                          f"(tradable=${tradable_cash:.2f})", flush=True)

                elif decision == "exit" and position is not None:
                    entry_price = position.avg_entry_price
                    exit_order = await executor.close_position(asset, price)
                    if exit_order is not None:
                        pnl_pct = (price - entry_price) / entry_price
                        if position.qty < 0:
                            pnl_pct = -pnl_pct
                        qty_abs = abs(position.qty)
                        profit_dollars = qty_abs * (price - entry_price) if position.qty > 0 \
                                         else qty_abs * (entry_price - price)
                        trade = {
                            "asset": asset,
                            "entry_price": entry_price,
                            "exit_price": price,
                            "exit_time": datetime.now(timezone.utc).isoformat(),
                            "direction": "long" if position.qty > 0 else "short",
                            "qty": qty_abs,
                            "pnl_pct": round(pnl_pct, 6),
                            "pnl_dollars": round(profit_dollars, 4),
                            "strategy_version": strategy.get("version", "?"),
                            "mode": executor.mode,
                            "exit_order_id": exit_order.order_id,
                        }
                        _log_trade(trade)
                        print(f"[EXIT]  {executor.mode} {asset} @ {price:.2f}  "
                              f"PnL={pnl_pct:.4%} (${profit_dollars:+.2f})", flush=True)

                        # Skim winning trades into vault + stock fund
                        if profit_dollars > 0:
                            await treasury.on_winning_trade(profit_dollars, executor, exit_order.order_id)
            except Exception as order_exc:
                # Recoverable: log, skip this action, keep the worker alive.
                print(f"[ORDER SKIPPED] {decision} on {asset} rejected: "
                      f"{str(order_exc)[:200]}", flush=True)
                _write_heartbeat("order_skipped", consecutive_failures,
                                 {"last_order_error": str(order_exc)[:200]})

            consecutive_failures = 0
            cash = await executor.fetch_cash()
            _write_heartbeat("ok", consecutive_failures, {
                "mode": executor.mode,
                "cash": round(cash, 2),
                "tradable_cash": round(max(0.0, cash - treasury.claimed_cash), 2),
                "vault_balance": treasury.vault_balance,
                "stock_fund_balance": treasury.stock_fund_balance,
                "has_open_position": position is not None,
            })

        except Exception as exc:
            consecutive_failures += 1
            print(f"[ERROR] tick failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {exc}", flush=True)
            _write_heartbeat("error", consecutive_failures, {"last_error": str(exc)[:200]})
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print("[CIRCUIT BREAK] too many consecutive failures — halting.", flush=True)
                raise SystemExit(1)

        elapsed = time.monotonic() - tick_start
        await asyncio.sleep(max(0, LOOP_INTERVAL_SECONDS - elapsed))

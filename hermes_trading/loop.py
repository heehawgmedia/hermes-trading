"""24/7 reliability loop — pulls data, evaluates strategy, logs paper trades."""
from __future__ import annotations
import asyncio
import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hermes_trading.adapters import price as price_adapter
from hermes_trading.adapters import onchain as onchain_adapter
from hermes_trading.adapters import news as news_adapter
from hermes_trading.adapters import macro as macro_adapter
from hermes_trading.score import score as compute_score

TRADES_PATH = Path("state/trades.jsonl")
STRATEGY_PATH = Path("state/strategy.yaml")
GOAL_PATH = Path("state/goal.yaml")
HEARTBEAT_PATH = Path("state/heartbeat.json")

MAX_CONSECUTIVE_FAILURES = 5
RETRY_ATTEMPTS = 3
LOOP_INTERVAL_SECONDS = 60


async def _fetch_with_retry(adapter, *args, **kwargs) -> Any:
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return await adapter.fetch(*args, **kwargs)
        except Exception as e:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            wait = 2 ** attempt
            await asyncio.sleep(wait)


def _load_strategy() -> dict:
    with open(STRATEGY_PATH) as f:
        return yaml.safe_load(f)


def _load_goal() -> dict:
    with open(GOAL_PATH) as f:
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
_open_position: dict | None = None


def _evaluate_strategy(strategy: dict, market: dict) -> str:
    """Returns 'enter', 'exit', or 'hold'."""
    global _open_position
    price = market["price"]
    entry_cfg = strategy.get("entry", {})
    indicator = entry_cfg.get("indicator", "rsi")
    threshold = float(entry_cfg.get("threshold", 30))
    direction = entry_cfg.get("direction", "long")
    stop_loss_pct = float(strategy.get("stop_loss_pct", 2.0)) / 100

    if indicator == "rsi":
        rsi = _compute_rsi(_price_history)
        if _open_position is None:
            if direction == "long" and rsi < threshold:
                return "enter"
        else:
            entry_price = _open_position["entry_price"]
            if direction == "long" and price < entry_price * (1 - stop_loss_pct):
                return "exit"
            if direction == "long" and rsi > 70:
                return "exit"
    return "hold"


def _log_trade(trade: dict) -> None:
    with open(TRADES_PATH, "a") as f:
        f.write(json.dumps(trade) + "\n")


def _write_heartbeat(status: str, consecutive_failures: int) -> None:
    data = {
        "status": status,
        "last_tick": datetime.now(timezone.utc).isoformat(),
        "consecutive_failures": consecutive_failures,
    }
    with open(HEARTBEAT_PATH, "w") as f:
        json.dump(data, f)


async def run_loop(asset: str) -> None:
    global _open_position, _price_history
    consecutive_failures = 0
    print(f"Booting hermes-trading worker — asset={asset}", flush=True)

    while True:
        tick_start = time.monotonic()
        try:
            market = await _fetch_with_retry(price_adapter, asset)
            _price_history.append(market["price"])
            if len(_price_history) > 200:
                _price_history = _price_history[-200:]

            strategy = _load_strategy()
            decision = _evaluate_strategy(strategy, market)

            if decision == "enter" and _open_position is None:
                _open_position = {
                    "entry_price": market["price"],
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "asset": asset,
                    "direction": strategy["entry"]["direction"],
                    "position_size_r": strategy.get("position_size_r", 0.5),
                }
                print(f"[ENTER] {asset} @ {market['price']:.2f}", flush=True)

            elif decision == "exit" and _open_position is not None:
                exit_price = market["price"]
                entry_price = _open_position["entry_price"]
                direction = _open_position["direction"]
                pnl_pct = (exit_price - entry_price) / entry_price
                if direction == "short":
                    pnl_pct = -pnl_pct

                trade = {
                    "asset": asset,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "entry_time": _open_position["entry_time"],
                    "exit_time": datetime.now(timezone.utc).isoformat(),
                    "direction": direction,
                    "pnl_pct": round(pnl_pct, 6),
                    "strategy_version": strategy.get("version", "unknown"),
                }
                _log_trade(trade)
                print(f"[EXIT] {asset} @ {exit_price:.2f}  PnL={pnl_pct:.4%}", flush=True)
                _open_position = None

            consecutive_failures = 0
            _write_heartbeat("ok", consecutive_failures)

        except Exception as exc:
            consecutive_failures += 1
            print(f"[ERROR] tick failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {exc}", flush=True)
            _write_heartbeat("error", consecutive_failures)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print("[CIRCUIT BREAK] too many consecutive failures — halting.", flush=True)
                raise SystemExit(1)

        elapsed = time.monotonic() - tick_start
        await asyncio.sleep(max(0, LOOP_INTERVAL_SECONDS - elapsed))

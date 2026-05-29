"""Entrypoint — parses asset from goal.yaml (overridable via --asset) and starts the loop."""
from __future__ import annotations
import argparse
import asyncio
import os
from pathlib import Path

import yaml

DEFAULT_GOAL = {
    "asset": "BTC/USDT",
    "target_return_30d": 0.10,
    "max_drawdown": 0.08,
    "min_sharpe": 1.2,
    "failure_below": -0.04,
    "reflection_every": 5,
    "one_variable_only": True,
}

DEFAULT_STRATEGY = {
    "version": "01",
    "entry": {"indicator": "rsi", "threshold": 32, "direction": "long"},
    "stop_loss_pct": 2.0,
    "position_size_r": 0.5,
}


def _init_state() -> None:
    """Create default state files on first boot if the volume is empty."""
    state_dir = Path("state")
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "history").mkdir(exist_ok=True)

    goal_path = state_dir / "goal.yaml"
    if not goal_path.exists():
        with open(goal_path, "w") as f:
            yaml.dump(DEFAULT_GOAL, f, default_flow_style=False, sort_keys=False)
        print("Initialized state/goal.yaml with defaults.", flush=True)

    strategy_path = state_dir / "strategy.yaml"
    if not strategy_path.exists():
        with open(strategy_path, "w") as f:
            yaml.dump(DEFAULT_STRATEGY, f, default_flow_style=False, sort_keys=False)
        print("Initialized state/strategy.yaml with defaults.", flush=True)

    for fname in ("trades.jsonl", "hypotheses.jsonl"):
        p = state_dir / fname
        if not p.exists():
            p.touch()

    heartbeat_path = state_dir / "heartbeat.json"
    if not heartbeat_path.exists():
        import json
        heartbeat_path.write_text(
            json.dumps({"status": "initializing", "last_tick": None, "consecutive_failures": 0})
        )


def _load_asset_from_goal() -> str:
    goal_path = Path("state/goal.yaml")
    if goal_path.exists():
        with open(goal_path) as f:
            goal = yaml.safe_load(f)
        return goal.get("asset", "BTC/USDT")
    return "BTC/USDT"


def main() -> None:
    parser = argparse.ArgumentParser(description="hermes-trading worker")
    parser.add_argument("--asset", default=None, help="Override asset from goal.yaml")
    args = parser.parse_args()

    _init_state()

    asset = args.asset or _load_asset_from_goal()

    mode = os.getenv("HERMES_TRADING_MODE", "paper")
    if mode == "live":
        accept = os.getenv("HERMES_TRADING_I_ACCEPT_RISK", "false").lower()
        if accept != "true":
            raise SystemExit(
                "Live mode requires HERMES_TRADING_I_ACCEPT_RISK=true in .env"
            )

    from hermes_trading.loop import run_loop
    asyncio.run(run_loop(asset))


if __name__ == "__main__":
    main()

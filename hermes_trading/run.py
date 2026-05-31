"""Entrypoint — parses asset from goal.yaml (overridable via --asset) and starts the loop."""
from __future__ import annotations
import argparse
import asyncio
from pathlib import Path

import yaml


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

    asset = args.asset or _load_asset_from_goal()

    from hermes_trading.dashboard import start_dashboard_in_background
    start_dashboard_in_background()

    from hermes_trading.loop import run_loop
    asyncio.run(run_loop(asset))


if __name__ == "__main__":
    main()

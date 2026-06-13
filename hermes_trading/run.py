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


def _migrate_state() -> None:
    """Ensure the persistent-volume copies of strategy.yaml / goal.yaml carry
    newly-added fields. The volume shadows the image's baked-in files, so new
    config keys must be injected here on boot or they never reach the worker."""
    # --- strategy.yaml: upgrade to the v04 candle+trend-filter engine ---
    sp = Path("state/strategy.yaml")
    if sp.exists():
        with open(sp) as f:
            strat = yaml.safe_load(f) or {}
        changed = []
        entry = strat.setdefault("entry", {})
        if "rsi_period" not in entry:
            entry["rsi_period"] = 14; changed.append("entry.rsi_period")
        if "overbought" not in entry:
            entry["overbought"] = 68; changed.append("entry.overbought")
        # Loosen the dip threshold now that the trend filter guards quality.
        if entry.get("threshold", 0) < 35:
            entry["threshold"] = 35; changed.append("entry.threshold=35")
        if "trend_filter" not in strat:
            strat["trend_filter"] = {"enabled": True, "sma_period": 50, "timeframe": "1H"}
            changed.append("trend_filter")
        # Fix reward:risk — bump the band-aid 1.5% TP to a real 2.5% target.
        if float(strat.get("take_profit_pct", 0)) < 2.5:
            strat["take_profit_pct"] = 2.5; changed.append("take_profit_pct=2.5")
        if "take_profit_pct" not in strat:
            strat["take_profit_pct"] = 2.5
        if changed:
            v = int(str(strat.get("version", "01")))
            strat["version"] = str(v + 1).zfill(2)
            with open(sp, "w") as f:
                yaml.dump(strat, f, default_flow_style=False, sort_keys=False)
            print(f"[migrate] strategy.yaml -> v{strat['version']} ({', '.join(changed)})", flush=True)

    # --- goal.yaml: ensure min_win_rate exists ---
    gp = Path("state/goal.yaml")
    if gp.exists():
        with open(gp) as f:
            goal = yaml.safe_load(f) or {}
        if "min_win_rate" not in goal:
            goal["min_win_rate"] = 0.55
            with open(gp, "w") as f:
                yaml.dump(goal, f, default_flow_style=False, sort_keys=False)
            print("[migrate] goal.yaml (added min_win_rate=0.55)", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="hermes-trading worker")
    parser.add_argument("--asset", default=None, help="Override asset from goal.yaml")
    args = parser.parse_args()

    _migrate_state()
    asset = args.asset or _load_asset_from_goal()

    from hermes_trading.dashboard import start_dashboard_in_background
    start_dashboard_in_background()

    from hermes_trading.loop import run_loop
    asyncio.run(run_loop(asset))


if __name__ == "__main__":
    main()

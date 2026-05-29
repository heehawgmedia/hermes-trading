"""Reflection cycle — deterministic fallback (--fallback) or Hermes-driven (--hermes)."""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

STRATEGY_PATH = Path("state/strategy.yaml")
GOAL_PATH = Path("state/goal.yaml")
HISTORY_DIR = Path("state/history")
HYPOTHESES_PATH = Path("state/hypotheses.jsonl")
TRADES_PATH = Path("state/trades.jsonl")


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _save_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _bump_version(strategy: dict) -> str:
    v = int(strategy.get("version", "01"))
    return str(v + 1).zfill(2)


def _save_history(strategy: dict) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    v = strategy.get("version", "01")
    dest = HISTORY_DIR / f"v{v}.yaml"
    _save_yaml(dest, strategy)


def _load_trades(n: int = 25) -> list[dict]:
    if not TRADES_PATH.exists():
        return []
    lines = TRADES_PATH.read_text().strip().splitlines()
    return [json.loads(l) for l in lines[-n:] if l]


def _append_hypothesis(hypothesis: dict) -> None:
    with open(HYPOTHESES_PATH, "a") as f:
        f.write(json.dumps(hypothesis) + "\n")


def reflect_fallback() -> None:
    strategy = _load_yaml(STRATEGY_PATH)
    goal = _load_yaml(GOAL_PATH)
    trades = _load_trades()

    if not trades:
        print("[reflect --fallback] No trades yet — nothing to reflect on.", flush=True)
        return

    returns = [t.get("pnl_pct", 0.0) for t in trades]
    realised = sum(returns)
    target = goal["target_return_30d"]
    max_dd_limit = goal["max_drawdown"]

    # compute simple drawdown
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)

    changed_var = None
    old_val = None
    new_val = None

    if realised < target:
        # loosen entry threshold by 2
        old_val = strategy["entry"]["threshold"]
        new_val = old_val + 2
        strategy["entry"]["threshold"] = new_val
        changed_var = "entry.threshold"
    elif max_dd > max_dd_limit:
        # tighten stop loss by 0.2
        old_val = strategy["stop_loss_pct"]
        new_val = round(old_val - 0.2, 2)
        strategy["stop_loss_pct"] = new_val
        changed_var = "stop_loss_pct"
    else:
        print("[reflect --fallback] Strategy within bounds — no change needed.", flush=True)
        return

    _save_history(_load_yaml(STRATEGY_PATH))
    new_version = _bump_version(strategy)
    strategy["version"] = new_version
    _save_yaml(STRATEGY_PATH, strategy)

    hypothesis = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "fallback",
        "changed_var": changed_var,
        "old_val": old_val,
        "new_val": new_val,
        "reason": f"realised={realised:.4f} target={target} dd={max_dd:.4f} dd_limit={max_dd_limit}",
        "version": new_version,
    }
    _append_hypothesis(hypothesis)
    print(f"[reflect --fallback] v{strategy['version']}: {changed_var} {old_val} -> {new_val}", flush=True)


def reflect_hermes() -> None:
    trades = _load_trades(25)
    strategy = _load_yaml(STRATEGY_PATH)
    goal = _load_yaml(GOAL_PATH)

    prompt = (
        f"You are the brain of a self-improving trading agent.\n\n"
        f"Goal:\n{yaml.dump(goal)}\n\n"
        f"Current strategy:\n{yaml.dump(strategy)}\n\n"
        f"Last {len(trades)} trades (jsonl):\n"
        + "\n".join(json.dumps(t) for t in trades)
        + "\n\nReflect. Choose exactly ONE variable in strategy.yaml to change. "
        "Output ONLY a JSON object with keys: changed_var, old_val, new_val, reason, confidence (0-1)."
    )

    try:
        result = subprocess.run(
            ["hermes", "--once", "--no-banner"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        raw = result.stdout.strip()
        hypothesis_data = json.loads(raw)
    except Exception as exc:
        print(f"[reflect --hermes] Hermes call failed: {exc}. Falling back to deterministic.", flush=True)
        reflect_fallback()
        return

    _save_history(_load_yaml(STRATEGY_PATH))
    strategy = _load_yaml(STRATEGY_PATH)
    new_version = _bump_version(strategy)

    changed_var = hypothesis_data["changed_var"]
    new_val = hypothesis_data["new_val"]

    # Apply the change by navigating the nested dict
    keys = changed_var.split(".")
    target = strategy
    for k in keys[:-1]:
        target = target[k]
    target[keys[-1]] = new_val

    strategy["version"] = new_version
    _save_yaml(STRATEGY_PATH, strategy)

    hypothesis_data.update({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "hermes",
        "version": new_version,
    })
    _append_hypothesis(hypothesis_data)
    print(f"[reflect --hermes] v{new_version}: {changed_var} → {new_val}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fallback", action="store_true")
    group.add_argument("--hermes", action="store_true")
    args = parser.parse_args()

    if args.fallback:
        reflect_fallback()
    else:
        reflect_hermes()


if __name__ == "__main__":
    main()

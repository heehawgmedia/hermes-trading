"""Score a list of trades against goal.yaml. Returns float in [-1, +1]."""
from __future__ import annotations
import math
from typing import List, Dict, Any

import numpy as np
import yaml


def _load_goal(path: str = "state/goal.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def score(trades: List[Dict[str, Any]], goal: dict | None = None) -> float:
    if goal is None:
        goal = _load_goal()

    if not trades:
        return 0.0

    returns = [t.get("pnl_pct", 0.0) for t in trades]
    arr = np.array(returns, dtype=float)

    realised = float(arr.sum())
    max_dd = _max_drawdown(arr)
    sharpe = _sharpe(arr)
    win_rate = float((arr > 0).sum()) / len(arr) if len(arr) else 0.0

    target = goal["target_return_30d"]
    max_dd_limit = goal["max_drawdown"]
    min_sharpe = goal["min_sharpe"]
    min_win_rate = goal.get("min_win_rate", 0.55)
    floor = goal.get("failure_below", -0.04)

    return_score = _clamp(realised / target, -1.0, 1.0) if target else 0.0
    dd_score = _clamp(1.0 - max_dd / max_dd_limit, -1.0, 1.0) if max_dd_limit else 0.0
    sharpe_score = _clamp(sharpe / min_sharpe, -1.0, 1.0) if min_sharpe else 0.0
    # Win-rate score: 0 at target, positive above, negative below. Scaled so that
    # hitting target = neutral-positive and falling to coin-flip (50%) hurts.
    win_score = _clamp((win_rate - min_win_rate) / max(min_win_rate, 1e-9), -1.0, 1.0) if min_win_rate else 0.0

    composite = (
        0.40 * return_score +
        0.25 * dd_score +
        0.15 * sharpe_score +
        0.20 * win_score
    )

    if realised < floor:
        composite = min(composite, -0.8)

    return round(_clamp(composite, -1.0, 1.0), 4)


def _max_drawdown(returns: np.ndarray) -> float:
    cumulative = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(cumulative)
    drawdowns = (peak - cumulative) / np.where(peak == 0, 1, peak)
    return float(drawdowns.max()) if len(drawdowns) else 0.0


def _sharpe(returns: np.ndarray, risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    excess = returns - risk_free / 252
    std = float(np.std(excess, ddof=1))
    if std == 0:
        return 0.0
    return float(np.mean(excess) / std * math.sqrt(252))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

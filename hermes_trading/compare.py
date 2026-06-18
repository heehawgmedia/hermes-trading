"""Backtest-vs-live comparison — detects whether the strategy's real Alpaca
performance still tracks its backtested expectation (out-of-sample decay check).

Runs weekly inside the worker (restart-safe, timestamp-gated) and is also
runnable on demand:  python -m hermes_trading.compare

For each run it:
  1. Reads recent LIVE closed trades from state/trades.jsonl (last N days).
  2. Replays the CURRENT strategy over the same recent window of real candles.
  3. Compares win rate / expectancy / profit factor / drawdown.
  4. Emits a verdict (edge holding / underperforming / insufficient data) and
     persists it to state/compare_report.json (+ append compare_history.jsonl).
"""
from __future__ import annotations
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

from hermes_trading.loop import _rsi_wilder, _sma
from hermes_trading.adapters import bars as bars_adapter

STATE = Path("state")
REPORT_PATH = STATE / "compare_report.json"
HISTORY_PATH = STATE / "compare_history.jsonl"


# ---------------------------------------------------------------- metrics ----
def _trade_metrics(returns: list[float], position_size_r: float = 0.3) -> dict:
    if not returns:
        return {"trades": 0}
    import numpy as np
    arr = np.array(returns, dtype=float)
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    equity = 1.0
    curve = [1.0]
    for r in returns:
        equity *= (1 + r * position_size_r)
        curve.append(equity)
    curve = np.array(curve)
    peak = np.maximum.accumulate(curve)
    max_dd = float(((peak - curve) / peak).max())
    sharpe = 0.0
    if len(arr) > 1 and arr.std(ddof=1) > 0:
        sharpe = float(arr.mean() / arr.std(ddof=1) * math.sqrt(len(arr)))
    # profit_factor = None (not inf) when there are no losses — inf is NOT valid
    # JSON and would 500 the dashboard endpoint when serialized.
    pf = (float(wins.sum() / -losses.sum())
          if len(losses) and losses.sum() != 0 else None)
    return {
        "trades": int(len(arr)),
        "win_rate": float((arr > 0).mean()),
        "expectancy": float(arr.mean()),
        "profit_factor": pf,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "total_return": float(equity - 1),
    }


# ----------------------------------------------------------------- live ------
def _live_returns(lookback_days: int, current_version: str | None) -> tuple[list[float], int]:
    path = STATE / "trades.jsonl"
    if not path.exists():
        return [], 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    returns, skipped = [], 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
        except Exception:
            continue
        # Only count REAL executor trades (have a mode), not synthetic seeds.
        if "mode" not in t:
            skipped += 1
            continue
        ts = t.get("exit_time", "")
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            when = None
        if when is not None and when < cutoff:
            continue
        returns.append(float(t.get("pnl_pct", 0.0)))
    return returns, skipped


# --------------------------------------------------------------- backtest ----
async def _backtest_returns(asset: str, strategy: dict, lookback_days: int) -> list[float]:
    entry = strategy.get("entry", {})
    threshold = float(entry.get("threshold", 45))
    rsi_period = int(entry.get("rsi_period", 14))
    overbought = float(entry.get("overbought", 60))
    trend = strategy.get("trend_filter", {}) or {}
    trend_enabled = bool(trend.get("enabled", True))
    sma_period = int(trend.get("sma_period", 200))
    timeframe = trend.get("timeframe", "1H")
    sl = float(strategy.get("stop_loss_pct", 3.0)) / 100
    tp = float(strategy.get("take_profit_pct", 5.0)) / 100

    # Need lookback candles + warmup for the SMA. 1H candles -> 24/day.
    tf_hours = {"1H": 1, "4H": 4, "1D": 24}.get(timeframe, 1)
    need = int(lookback_days * 24 / tf_hours) + sma_period + rsi_period + 10
    data = await bars_adapter.fetch(asset, timeframe, min(need, 10000))
    closes, highs, lows = data["closes"], data["highs"], data["lows"]

    returns = []
    in_pos = False
    entry_price = 0.0
    warmup = max(sma_period, rsi_period) + 2
    for i in range(warmup, len(closes)):
        window = closes[: i + 1]
        price = closes[i]
        rsi = _rsi_wilder(window, rsi_period)
        sma = _sma(window, sma_period)
        in_uptrend = (sma is not None) and (price > sma)
        if not in_pos:
            if rsi < threshold and (not trend_enabled or in_uptrend):
                in_pos = True
                entry_price = price
        else:
            stop_p, tp_p = entry_price * (1 - sl), entry_price * (1 + tp)
            ex = None
            if lows[i] <= stop_p:
                ex = stop_p
            elif highs[i] >= tp_p:
                ex = tp_p
            elif rsi >= overbought:
                ex = price
            if ex is not None:
                returns.append((ex - entry_price) / entry_price)
                in_pos = False
    return returns


# ---------------------------------------------------------------- verdict ----
def _verdict(live: dict, bt: dict) -> dict:
    flags = []
    if live.get("trades", 0) < 3:
        return {"status": "insufficient_live_data",
                "headline": f"Only {live.get('trades', 0)} live trade(s) in window — "
                            "need ≥3 before judging the edge.",
                "flags": flags}

    # Win-rate gap
    wr_gap = bt["win_rate"] - live["win_rate"]
    if wr_gap > 0.15:
        flags.append(f"Win rate {live['win_rate']*100:.0f}% is {wr_gap*100:.0f}pts below "
                     f"backtest {bt['win_rate']*100:.0f}%.")
    # Expectancy sign mismatch (the big one)
    if bt["expectancy"] > 0 and live["expectancy"] <= 0:
        flags.append(f"Live expectancy {live['expectancy']*100:+.2f}%/trade is negative "
                     f"while backtest is {bt['expectancy']*100:+.2f}% — edge NOT holding "
                     "out-of-sample.")
    # Drawdown blowout
    if live["max_drawdown"] > max(bt["max_drawdown"] * 1.5, 0.04):
        flags.append(f"Live drawdown {live['max_drawdown']*100:.1f}% exceeds "
                     f"1.5× backtest {bt['max_drawdown']*100:.1f}%.")

    if not flags:
        status = "edge_holding"
        headline = (f"Live tracking backtest: win {live['win_rate']*100:.0f}% vs "
                    f"{bt['win_rate']*100:.0f}%, expectancy {live['expectancy']*100:+.2f}% vs "
                    f"{bt['expectancy']*100:+.2f}%. Edge holding.")
    elif any("NOT holding" in f for f in flags):
        status = "underperforming"
        headline = "LIVE UNDERPERFORMING the backtest — investigate before trusting the edge."
    else:
        status = "watch"
        headline = "Live diverging from backtest — watch closely."
    return {"status": status, "headline": headline, "flags": flags}


# ------------------------------------------------------------------ run ------
async def run_comparison(asset: str | None = None, lookback_days: int = 30) -> dict:
    goal = {}
    gp = STATE / "goal.yaml"
    if gp.exists():
        goal = yaml.safe_load(gp.read_text()) or {}
    asset = asset or goal.get("asset", "BTC/USDT")
    strategy = yaml.safe_load((STATE / "strategy.yaml").read_text()) or {}
    psize = float(strategy.get("position_size_r", 0.3))
    version = strategy.get("version")

    live_ret, seeds_skipped = _live_returns(lookback_days, version)
    try:
        bt_ret = await _backtest_returns(asset, strategy, lookback_days)
    except Exception as e:
        bt_ret = []
        print(f"[compare] backtest leg failed: {str(e)[:160]}", flush=True)

    live_m = _trade_metrics(live_ret, psize)
    bt_m = _trade_metrics(bt_ret, psize)
    verdict = _verdict(live_m, bt_m)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset": asset,
        "strategy_version": version,
        "lookback_days": lookback_days,
        "live": live_m,
        "backtest": bt_m,
        "verdict": verdict,
        "seed_trades_skipped": seeds_skipped,
    }
    STATE.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    with open(HISTORY_PATH, "a") as f:
        f.write(json.dumps(report) + "\n")
    return report


def _fmt_row(label: str, m: dict) -> str:
    if m.get("trades", 0) == 0:
        return f"  {label:9} (no trades)"
    pf = m["profit_factor"]
    pf_s = "inf" if pf is None else f"{pf:.2f}"
    return (f"  {label:9} n={m['trades']:>3}  win={m['win_rate']*100:5.1f}%  "
            f"exp={m['expectancy']*100:+.2f}%/t  PF={pf_s:>4}  "
            f"DD={m['max_drawdown']*100:4.1f}%  ret={m['total_return']*100:+6.1f}%")


def print_report(r: dict) -> None:
    print(f"\n=== BACKTEST vs LIVE  ({r['asset']} · strategy v{r['strategy_version']} · "
          f"last {r['lookback_days']}d) ===")
    print(_fmt_row("BACKTEST", r["backtest"]))
    print(_fmt_row("LIVE", r["live"]))
    v = r["verdict"]
    print(f"\n  VERDICT [{v['status'].upper()}]: {v['headline']}")
    for f in v.get("flags", []):
        print(f"    - {f}")
    print()


async def weekly_loop(asset: str | None = None, interval_days: int = 7,
                      check_every_hours: int = 6) -> None:
    """Restart-safe weekly scheduler: runs a comparison when ≥interval_days have
    elapsed since the last persisted report, else waits. Survives redeploys
    because the cadence is anchored to the report timestamp on the volume."""
    import asyncio
    first = True
    while True:
        try:
            last = None
            if REPORT_PATH.exists():
                try:
                    last = datetime.fromisoformat(json.loads(REPORT_PATH.read_text())["generated_at"])
                except Exception:
                    last = None
            # Always refresh once on boot (cheap, and overwrites any stale report),
            # then settle into the weekly cadence.
            due = first or last is None or (datetime.now(timezone.utc) - last) >= timedelta(days=interval_days)
            first = False
            if due:
                r = await run_comparison(asset)
                v = r["verdict"]
                print(f"[compare] weekly report [{v['status']}]: {v['headline']}", flush=True)
        except Exception as e:
            print(f"[compare] weekly loop error: {str(e)[:160]}", flush=True)
        await asyncio.sleep(check_every_hours * 3600)


def main() -> None:
    import asyncio
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    r = asyncio.run(run_comparison(lookback_days=days))
    print_report(r)


if __name__ == "__main__":
    main()

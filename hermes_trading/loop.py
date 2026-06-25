"""24/7 reliability loop — pulls data, evaluates strategy, delegates execution."""
from __future__ import annotations
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from hermes_trading.adapters import price as price_adapter
from hermes_trading.adapters import bars as bars_adapter
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


def _rsi_wilder(closes: list[float], period: int = 14) -> float:
    """Wilder-smoothed RSI on real candle closes. Returns 50.0 if insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    import numpy as np
    arr = np.asarray(closes, dtype=float)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # Seed with simple average of first `period`, then Wilder-smooth the rest.
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _sma(closes: list[float], period: int) -> float | None:
    """Simple moving average of the last `period` closes, or None if too short."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _evaluate_strategy(strategy: dict, price: float, closes: list[float],
                       has_position: bool, entry_price: float | None) -> tuple[str, dict]:
    """Returns (decision, signals) where decision is 'enter'|'exit'|'hold'.
    `closes` are real candle closes (oldest→newest). `price` is the live fill price."""
    entry_cfg = strategy.get("entry", {})
    indicator = entry_cfg.get("indicator", "rsi")
    threshold = float(entry_cfg.get("threshold", 30))
    rsi_period = int(entry_cfg.get("rsi_period", 14))
    overbought = float(entry_cfg.get("overbought", 70))
    direction = entry_cfg.get("direction", "long")
    stop_loss_pct = float(strategy.get("stop_loss_pct", 2.0)) / 100
    take_profit_pct = float(strategy.get("take_profit_pct", 0)) / 100  # 0 = disabled

    trend_cfg = strategy.get("trend_filter", {}) or {}
    trend_enabled = bool(trend_cfg.get("enabled", True))
    trend_period = int(trend_cfg.get("sma_period", 50))

    rsi = _rsi_wilder(closes, rsi_period)
    trend_sma = _sma(closes, trend_period)
    # Uptrend = price above its longer moving average. If we don't have enough
    # candles for the trend SMA yet, stay flat rather than guess.
    in_uptrend = (trend_sma is not None) and (price > trend_sma)
    signals = {"rsi": round(rsi, 2),
               "trend_sma": round(trend_sma, 2) if trend_sma else None,
               "in_uptrend": in_uptrend}

    if indicator != "rsi":
        return "hold", signals

    if not has_position:
        # Long ONLY on an oversold pullback WITHIN an uptrend — never buy a
        # falling knife in a downtrend. This is the core fix.
        if direction == "long" and rsi < threshold:
            if not trend_enabled or in_uptrend:
                return "enter", signals
        return "hold", signals

    # Manage the open position.
    assert entry_price is not None
    if direction == "long":
        if price <= entry_price * (1 - stop_loss_pct):
            return "exit", signals  # stop loss
        if take_profit_pct > 0 and price >= entry_price * (1 + take_profit_pct):
            return "exit", signals  # take profit (price target)
        if rsi >= overbought:
            return "exit", signals  # momentum exhausted
    return "hold", signals


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


def _load_watchlist(fallback: str) -> list[str]:
    """Assets to scan for entries. From goal.yaml `watchlist`, else [primary asset]."""
    try:
        with open(GOAL_PATH) as f:
            goal = yaml.safe_load(f) or {}
        wl = goal.get("watchlist")
        if wl:
            return list(dict.fromkeys(wl))  # de-dupe, preserve order
        return [goal.get("asset", fallback)]
    except Exception:
        return [fallback]


async def _snapshot(asset: str, timeframe: str) -> tuple[float, list[float], str]:
    """Real candles + fill price for one asset, both from Alpaca bars.

    Using the latest candle close as the fill reference (rather than a separate
    CoinGecko spot call) keeps the price source consistent with the indicators,
    matches how the backtest prices fills, and works for every Alpaca coin
    (CoinGecko's spot endpoint only mapped a handful of tickers)."""
    candle_data = await _fetch_with_retry(bars_adapter, asset, timeframe, 300)
    return float(candle_data["last_price"]), candle_data["closes"], candle_data["source"]


async def run_loop(asset: str) -> None:
    consecutive_failures = 0
    executor = get_executor()
    treasury = Treasury()
    watchlist = _load_watchlist(asset)
    print(
        f"Booting hermes-trading worker — watchlist={watchlist} mode={executor.mode} "
        f"vault_skim={treasury.config.vault_skim_pct*100:.0f}% "
        f"fund_skim={treasury.config.stock_fund_skim_pct*100:.0f}% "
        f"dca_threshold=${treasury.config.stock_fund_dca_threshold:.0f} "
        f"dca_ticker={treasury.config.dca_ticker or '(unset)'}",
        flush=True,
    )

    # Clean slate: clear any stale orders across the whole watchlist.
    for a in watchlist:
        try:
            n = await executor.cancel_open_orders(a)
            if n:
                print(f"[startup] cancelled {n} stale open order(s) for {a}", flush=True)
        except Exception as e:
            print(f"[startup] order cleanup skipped for {a}: {e}", flush=True)

    # Weekly backtest-vs-live edge check runs as a background task (restart-safe).
    try:
        from hermes_trading.compare import weekly_loop
        asyncio.create_task(weekly_loop(asset))
        print("[startup] weekly backtest-vs-live comparison scheduled", flush=True)
    except Exception as e:
        print(f"[startup] comparison scheduler skipped: {e}", flush=True)

    while True:
        tick_start = time.monotonic()
        try:
            strategy = _load_strategy()
            timeframe = strategy.get("trend_filter", {}).get("timeframe", "1H")
            watchlist = _load_watchlist(asset)  # re-read so Hermes edits take effect

            # Single position at a time: if we hold one, manage it; else scan for the
            # best new entry across the watchlist.
            held = [p for p in await executor.fetch_all_positions() if p.asset in watchlist]

            try:
                if held:
                    pos = held[0]
                    active = pos.asset
                    price, closes, source = await _snapshot(active, timeframe)
                    decision, signals = _evaluate_strategy(
                        strategy, price, closes,
                        has_position=True, entry_price=pos.avg_entry_price)

                    if decision == "exit":
                        exit_order = await executor.close_position(active, price)
                        if exit_order is not None:
                            entry_price = pos.avg_entry_price
                            pnl_pct = (price - entry_price) / entry_price
                            if pos.qty < 0:
                                pnl_pct = -pnl_pct
                            qty_abs = abs(pos.qty)
                            profit_dollars = (qty_abs * (price - entry_price) if pos.qty > 0
                                              else qty_abs * (entry_price - price))
                            trade = {
                                "asset": active, "entry_price": entry_price, "exit_price": price,
                                "exit_time": datetime.now(timezone.utc).isoformat(),
                                "direction": "long" if pos.qty > 0 else "short",
                                "qty": qty_abs, "pnl_pct": round(pnl_pct, 6),
                                "pnl_dollars": round(profit_dollars, 4),
                                "strategy_version": strategy.get("version", "?"),
                                "mode": executor.mode, "exit_order_id": exit_order.order_id,
                            }
                            _log_trade(trade)
                            print(f"[EXIT]  {executor.mode} {active} @ {price:.2f}  "
                                  f"PnL={pnl_pct:.4%} (${profit_dollars:+.2f})", flush=True)
                            if profit_dollars > 0:
                                await treasury.on_winning_trade(profit_dollars, executor, exit_order.order_id)
                        active_signals = signals
                    else:
                        print(f"[HOLD]  {active} {price:.2f} rsi={signals['rsi']} "
                              f"uptrend={signals['in_uptrend']} src={source}", flush=True)
                        active_signals = signals

                else:
                    # FLAT → scan watchlist, pick the best pullback-in-uptrend setup
                    # (most oversold = lowest RSI among assets that pass the trend filter).
                    candidates = []
                    scan = []
                    for a in watchlist:
                        try:
                            price, closes, source = await _snapshot(a, timeframe)
                        except Exception as se:
                            print(f"[scan] {a} data error: {str(se)[:80]}", flush=True)
                            continue
                        decision, signals = _evaluate_strategy(
                            strategy, price, closes, has_position=False, entry_price=None)
                        scan.append(f"{a}:rsi{signals['rsi']}/{'up' if signals['in_uptrend'] else 'dn'}")
                        if decision == "enter":
                            candidates.append((signals["rsi"], a, price, signals))
                    print(f"[SCAN] flat — {'  '.join(scan)}", flush=True)

                    active_signals = None
                    if candidates:
                        candidates.sort(key=lambda c: c[0])  # lowest RSI first
                        _, chosen, price, active_signals = candidates[0]
                        pending = await executor.count_open_orders(chosen)
                        if pending > 0:
                            await executor.cancel_open_orders(chosen)
                            print(f"[ENTER SKIPPED] stale pending order(s) for {chosen}", flush=True)
                        else:
                            position_size_r = float(strategy.get("position_size_r", 0.3))
                            tradable_cash = max(0.0, (await executor.fetch_cash()) - treasury.claimed_cash)
                            order = await executor.place_market_order(
                                chosen, "buy", position_size_r=position_size_r,
                                current_price=price, tradable_cash=tradable_cash)
                            print(f"[ENTER] {executor.mode} {chosen} buy qty={order.qty:.6f} @ {price:.2f} "
                                  f"(rsi={active_signals['rsi']}, tradable=${tradable_cash:.2f})", flush=True)
            except Exception as order_exc:
                print(f"[ORDER SKIPPED] action rejected: {str(order_exc)[:200]}", flush=True)
                _write_heartbeat("order_skipped", consecutive_failures,
                                 {"last_order_error": str(order_exc)[:200]})
                active_signals = None

            consecutive_failures = 0
            cash = await executor.fetch_cash()
            hb = {
                "mode": executor.mode,
                "cash": round(cash, 2),
                "tradable_cash": round(max(0.0, cash - treasury.claimed_cash), 2),
                "vault_balance": treasury.vault_balance,
                "stock_fund_balance": treasury.stock_fund_balance,
                "has_open_position": bool(held),
                "active_asset": held[0].asset if held else None,
                "watchlist": watchlist,
            }
            if active_signals:
                hb.update({"rsi": active_signals["rsi"],
                           "trend_sma": active_signals["trend_sma"],
                           "in_uptrend": active_signals["in_uptrend"]})
            _write_heartbeat("ok", consecutive_failures, hb)

        except Exception as exc:
            consecutive_failures += 1
            print(f"[ERROR] tick failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {exc}", flush=True)
            _write_heartbeat("error", consecutive_failures, {"last_error": str(exc)[:200]})
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print("[CIRCUIT BREAK] too many consecutive failures — halting.", flush=True)
                raise SystemExit(1)

        elapsed = time.monotonic() - tick_start
        await asyncio.sleep(max(0, LOOP_INTERVAL_SECONDS - elapsed))

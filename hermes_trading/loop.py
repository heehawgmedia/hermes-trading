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


async def run_loop(asset: str) -> None:
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

    # Clean slate: clear any stale orders left by a previous run so we never
    # resume into a pile of stuck pending orders.
    try:
        n = await executor.cancel_open_orders(asset)
        if n:
            print(f"[startup] cancelled {n} stale open order(s) for {asset}", flush=True)
    except Exception as e:
        print(f"[startup] order cleanup skipped: {e}", flush=True)

    while True:
        tick_start = time.monotonic()
        try:
            strategy = _load_strategy()
            timeframe = strategy.get("trend_filter", {}).get("timeframe", "1H")
            # Real OHLC candles drive the indicators; live spot is the fill reference.
            candle_data = await _fetch_with_retry(bars_adapter, asset, timeframe, 300)
            closes = candle_data["closes"]
            market = await _fetch_with_retry(price_adapter, asset)
            price = market["price"]

            position = await executor.fetch_position(asset)
            decision, signals = _evaluate_strategy(
                strategy, price, closes,
                has_position=position is not None,
                entry_price=position.avg_entry_price if position else None,
            )

            # --- Order actions are RECOVERABLE: a rejected/failed order logs and
            # --- continues. It must NOT trip the connectivity circuit breaker,
            # --- otherwise one Alpaca business-rule rejection crashes the worker.
            try:
                if decision == "enter":
                    # GUARD against the runaway-stacking bug: if an order is already
                    # working for this asset, a market order should have filled within
                    # a tick. If it's still open, it's stale — cancel it and re-evaluate
                    # next tick rather than stacking another buy on top.
                    pending = await executor.count_open_orders(asset)
                    if pending > 0:
                        cancelled = await executor.cancel_open_orders(asset)
                        print(f"[ENTER SKIPPED] {pending} stale pending order(s) for {asset}; "
                              f"cancelled {cancelled}, re-evaluating next tick", flush=True)
                    else:
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
            if decision == "hold":
                print(f"[TICK] {asset} {price:.2f} rsi={signals['rsi']} "
                      f"uptrend={signals['in_uptrend']} pos={position is not None} "
                      f"src={candle_data['source']}", flush=True)
            _write_heartbeat("ok", consecutive_failures, {
                "mode": executor.mode,
                "cash": round(cash, 2),
                "tradable_cash": round(max(0.0, cash - treasury.claimed_cash), 2),
                "vault_balance": treasury.vault_balance,
                "stock_fund_balance": treasury.stock_fund_balance,
                "has_open_position": position is not None,
                "rsi": signals["rsi"],
                "trend_sma": signals["trend_sma"],
                "in_uptrend": signals["in_uptrend"],
                "candle_source": candle_data["source"],
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

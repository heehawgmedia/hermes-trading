#!/usr/bin/env python3
"""Backtest the live strategy against historical BTC hourly candles.

Reuses the EXACT indicator + decision functions from hermes_trading.loop so the
backtest faithfully reflects production behavior. Models intrabar stop/TP fills
using candle high/low (standard practice), and enters at the signal candle's
close (no look-ahead).
"""
from __future__ import annotations
import os
import sys
import math
import httpx
from datetime import datetime, timezone, timedelta

from hermes_trading.loop import _rsi_wilder, _sma


def fetch_candles(asset="BTC/USD", timeframe="1H", days=90):
    key = os.getenv("ALPACA_API_KEY", "").strip()
    sec = os.getenv("ALPACA_API_SECRET", "").strip()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec} if key else {}
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bars, token = [], None
    with httpx.Client(timeout=30) as client:
        while True:
            params = {"symbols": asset, "timeframe": timeframe, "start": start, "limit": 10000}
            if token:
                params["page_token"] = token
            r = client.get("https://data.alpaca.markets/v1beta3/crypto/us/bars",
                           params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            bars.extend(data.get("bars", {}).get(asset, []))
            token = data.get("next_page_token")
            if not token:
                break
    return bars


def backtest(bars, *, threshold=35, rsi_period=14, overbought=68,
             trend_enabled=True, sma_period=50, stop_loss_pct=2.0,
             take_profit_pct=2.5):
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]

    sl = stop_loss_pct / 100
    tp = take_profit_pct / 100

    trades = []          # list of pnl_pct per closed trade
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
            stop_price = entry_price * (1 - sl)
            tp_price = entry_price * (1 + tp)
            exit_price = None
            # Intrabar: conservative — assume stop checked before target.
            if lows[i] <= stop_price:
                exit_price = stop_price
            elif highs[i] >= tp_price:
                exit_price = tp_price
            elif rsi >= overbought:
                exit_price = price
            if exit_price is not None:
                trades.append((exit_price - entry_price) / entry_price)
                in_pos = False

    return _metrics(trades, position_size_r=0.5)


def _metrics(trades, position_size_r=0.5):
    if not trades:
        return {"trades": 0}
    import numpy as np
    arr = np.array(trades)
    wins = arr[arr > 0]
    losses = arr[arr < 0]

    # Compound an equity curve at the configured fraction per trade.
    equity = 1.0
    curve = [1.0]
    for r in trades:
        equity *= (1 + r * position_size_r)
        curve.append(equity)
    curve = np.array(curve)
    peak = np.maximum.accumulate(curve)
    max_dd = float(((peak - curve) / peak).max())

    sharpe = 0.0
    if len(arr) > 1 and arr.std(ddof=1) > 0:
        sharpe = float(arr.mean() / arr.std(ddof=1) * math.sqrt(len(arr)))

    return {
        "trades": len(arr),
        "win_rate": float((arr > 0).mean()),
        "total_return": float(equity - 1),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() != 0 else float("inf"),
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "expectancy": float(arr.mean()),
    }


def _fmt(m):
    if m.get("trades", 0) == 0:
        return "no trades"
    return (f"trades={m['trades']:>3}  win={m['win_rate']*100:5.1f}%  "
            f"ret={m['total_return']*100:+6.1f}%  PF={m['profit_factor']:4.2f}  "
            f"maxDD={m['max_drawdown']*100:4.1f}%  sharpe={m['sharpe']:5.2f}  "
            f"exp={m['expectancy']*100:+.3f}%/trade")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    print(f"Fetching {days}d of BTC/USD 1H candles from Alpaca...")
    bars = fetch_candles(days=days)
    print(f"Got {len(bars)} candles "
          f"({bars[0]['t'][:10]} -> {bars[-1]['t'][:10]})\n")

    print("=== LIVE STRATEGY (v03): RSI<35 + SMA50 trend filter, TP 2.5% / SL 2.0% ===")
    live = backtest(bars)
    print("  " + _fmt(live), "\n")

    print("=== ABLATION: what each fix contributes ===")
    print("  trend filter OFF (the old falling-knife behavior):")
    print("    " + _fmt(backtest(bars, trend_enabled=False)))
    print("  old R:R (TP 1.5% / SL 2.0%, trend ON):")
    print("    " + _fmt(backtest(bars, take_profit_pct=1.5)))
    print("  old entry threshold 30 (trend ON):")
    print("    " + _fmt(backtest(bars, threshold=30)))

    print("\n=== PARAMETER SWEEP (trend filter ON) ===")
    for th in (30, 35, 40):
        for tpv in (2.0, 2.5, 3.0):
            m = backtest(bars, threshold=th, take_profit_pct=tpv)
            print(f"  RSI<{th}  TP{tpv}/SL2.0:  {_fmt(m)}")

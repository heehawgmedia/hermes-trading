#!/usr/bin/env python3
"""Explore multiple strategy archetypes against historical BTC candles to find
a construction that actually trades AND has positive expectancy. Crypto trends,
so we test trend-following and breakout alongside (corrected) mean-reversion."""
from __future__ import annotations
import os, sys, math
import httpx
import numpy as np
from datetime import datetime, timezone, timedelta
from hermes_trading.loop import _rsi_wilder, _sma


def fetch(asset="BTC/USD", timeframe="1H", days=120):
    key = os.getenv("ALPACA_API_KEY", "").strip()
    sec = os.getenv("ALPACA_API_SECRET", "").strip()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec} if key else {}
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bars, token = [], None
    with httpx.Client(timeout=30) as c:
        while True:
            p = {"symbols": asset, "timeframe": timeframe, "start": start, "limit": 10000}
            if token: p["page_token"] = token
            r = c.get("https://data.alpaca.markets/v1beta3/crypto/us/bars", params=p, headers=headers)
            r.raise_for_status(); d = r.json()
            bars.extend(d.get("bars", {}).get(asset, []))
            token = d.get("next_page_token")
            if not token: break
    return bars


def metrics(trades, psize=0.5):
    if not trades: return {"trades": 0}
    arr = np.array(trades); wins = arr[arr > 0]; losses = arr[arr < 0]
    eq = 1.0; curve = [1.0]
    for r in trades:
        eq *= (1 + r * psize); curve.append(eq)
    curve = np.array(curve); peak = np.maximum.accumulate(curve)
    mdd = float(((peak - curve) / peak).max())
    sharpe = float(arr.mean()/arr.std(ddof=1)*math.sqrt(len(arr))) if len(arr) > 1 and arr.std(ddof=1) > 0 else 0.0
    return {"trades": len(arr), "win": float((arr > 0).mean()), "ret": float(eq-1),
            "pf": float(wins.sum()/-losses.sum()) if len(losses) and losses.sum() else float("inf"),
            "mdd": mdd, "sharpe": sharpe, "exp": float(arr.mean())}


def _run(closes, highs, lows, signal_fn, sl_pct, tp_pct, warmup, use_signal_exit=True):
    """Generic engine. signal_fn(i, window) -> ('enter'|'exit'|'hold'-ish via bool tuple).
    Returns list of trade pnl_pct. Stop/TP modelled intrabar via high/low."""
    sl, tp = sl_pct/100, tp_pct/100
    trades = []; in_pos = False; entry = 0.0
    for i in range(warmup, len(closes)):
        price = closes[i]
        want_long, want_flat = signal_fn(i)
        if not in_pos:
            if want_long:
                in_pos = True; entry = price
        else:
            stop_p, tp_p = entry*(1-sl), entry*(1+tp)
            ex = None
            if lows[i] <= stop_p: ex = stop_p
            elif highs[i] >= tp_p: ex = tp_p
            elif use_signal_exit and want_flat: ex = price
            if ex is not None:
                trades.append((ex-entry)/entry); in_pos = False
    return trades


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    print(f"Fetching {days}d BTC/USD 1H candles...")
    bars = fetch(days=days)
    closes = [float(b["c"]) for b in bars]; highs = [float(b["h"]) for b in bars]; lows = [float(b["l"]) for b in bars]
    print(f"Got {len(bars)} candles ({bars[0]['t'][:10]} -> {bars[-1]['t'][:10]})\n")

    def fmt(name, m):
        if m.get("trades", 0) == 0: return f"{name:42} no trades"
        return (f"{name:42} n={m['trades']:>3} win={m['win']*100:5.1f}% ret={m['ret']*100:+7.1f}% "
                f"PF={m['pf']:4.2f} DD={m['mdd']*100:4.1f}% sharpe={m['sharpe']:6.2f} exp={m['exp']*100:+.2f}%")

    # --- Precompute rolling SMAs/EMAs/RSI arrays for speed ---
    def sma_at(i, p): return _sma(closes[:i+1], p)
    def rsi_at(i, p=14): return _rsi_wilder(closes[:i+1], p)

    results = []

    # 1) TREND-FOLLOWING: SMA fast/slow crossover (ride trends, exit on cross)
    for fast, slow in [(10, 30), (20, 50), (20, 100), (50, 200)]:
        def sig(i, f=fast, s=slow):
            sf, ss = sma_at(i, f), sma_at(i, s)
            if sf is None or ss is None: return (False, False)
            return (sf > ss, sf < ss)
        m = metrics(_run(closes, highs, lows, sig, sl_pct=4.0, tp_pct=8.0, warmup=slow+2))
        results.append((f"TREND SMA{fast}/{slow} cross (SL4/TP8)", m))

    # 2) BREAKOUT: enter on N-period high breakout, exit on M-period low
    for hi, lo in [(48, 24), (72, 24), (24, 12)]:
        def sig(i, hh=hi, ll=lo):
            if i < hh: return (False, False)
            recent_high = max(highs[i-hh:i]); recent_low = min(lows[i-ll:i])
            return (closes[i] >= recent_high, closes[i] <= recent_low)
        m = metrics(_run(closes, highs, lows, sig, sl_pct=4.0, tp_pct=10.0, warmup=hi+2))
        results.append((f"BREAKOUT {hi}h-high / {lo}h-low (SL4/TP10)", m))

    # 3) MOMENTUM: long when price>SMA50 and RSI rising through 50; exit RSI<45
    for trend_p in [50, 100]:
        def sig(i, tp_=trend_p):
            s = sma_at(i, tp_); r = rsi_at(i)
            if s is None: return (False, False)
            return (closes[i] > s and r > 52, r < 45)
        m = metrics(_run(closes, highs, lows, sig, sl_pct=3.0, tp_pct=6.0, warmup=trend_p+2))
        results.append((f"MOMENTUM px>SMA{trend_p} & RSI>52 (SL3/TP6)", m))

    # 4) PULLBACK in higher-TF uptrend: trend = price>SMA200, dip = RSI<45
    for trend_p, th in [(150, 45), (200, 45), (200, 40)]:
        def sig(i, tp_=trend_p, t=th):
            s = sma_at(i, tp_); r = rsi_at(i)
            if s is None: return (False, False)
            return (closes[i] > s and r < t, r > 60)
        m = metrics(_run(closes, highs, lows, sig, sl_pct=3.0, tp_pct=5.0, warmup=trend_p+2))
        results.append((f"PULLBACK px>SMA{trend_p} & RSI<{th} (SL3/TP5)", m))

    # 5) Buy-and-hold benchmark
    bh = (closes[-1]-closes[0])/closes[0]
    print("=== STRATEGY ARCHETYPES (ranked by total return) ===")
    for name, m in sorted(results, key=lambda x: x[1].get("ret", -9), reverse=True):
        print("  " + fmt(name, m))
    print(f"\n  Buy & hold BTC over period: {bh*100:+.1f}%")


if __name__ == "__main__":
    main()

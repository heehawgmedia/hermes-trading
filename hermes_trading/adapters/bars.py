"""OHLC candle adapter — real market candles for indicator computation.

Primary source: Alpaca crypto bars (reliable OHLC, same keys we already use).
Fallback: CoinGecko market_chart (no key, but coarser).

Why candles instead of sampled spot prices: indicators like RSI/SMA are only
meaningful on real OHLC closes. Sampling a cached spot price every 60s produces
a noisy, restart-fragile pseudo-series that makes the signal worthless.
"""
from __future__ import annotations
import os
import httpx

SCHEMA_VERSION = "1.0"


class SchemaError(Exception):
    pass


def _to_alpaca_symbol(asset: str) -> str:
    base, _, _ = asset.partition("/")
    return f"{base}/USD"


_COINGECKO_IDS = {
    "BTC/USDT": "bitcoin", "ETH/USDT": "ethereum", "SOL/USDT": "solana",
    "BNB/USDT": "binancecoin", "XRP/USDT": "ripple",
}


async def fetch_bars(asset: str = "BTC/USDT", timeframe: str = "1H", limit: int = 300) -> dict:
    """Returns {'closes': [...], 'highs': [...], 'lows': [...], 'last_price': float,
    'source': str}. Closes are oldest→newest real candle closes."""
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_API_SECRET", "").strip()

    # --- Primary: Alpaca crypto bars (real OHLC) ---
    try:
        sym = _to_alpaca_symbol(asset)
        headers = {}
        if key and secret:
            headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://data.alpaca.markets/v1beta3/crypto/us/bars",
                params={"symbols": sym, "timeframe": timeframe, "limit": limit},
                headers=headers,
            )
            r.raise_for_status()
            data = r.json()
        bars = data.get("bars", {}).get(sym, [])
        if bars and len(bars) >= 20:
            closes = [float(b["c"]) for b in bars]
            highs = [float(b["h"]) for b in bars]
            lows = [float(b["l"]) for b in bars]
            return {
                "schema_version": SCHEMA_VERSION,
                "asset": asset,
                "closes": closes,
                "highs": highs,
                "lows": lows,
                "last_price": closes[-1],
                "source": "alpaca-bars",
            }
    except Exception:
        pass  # fall through to CoinGecko

    # --- Fallback: CoinGecko market_chart (hourly closes) ---
    coin_id = _COINGECKO_IDS.get(asset.upper(), asset.split("/")[0].lower())
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": "14", "interval": "hourly"},
        )
        r.raise_for_status()
        data = r.json()
    prices = [float(p[1]) for p in data.get("prices", [])]
    if len(prices) < 20:
        raise SchemaError(f"bars adapter: insufficient candle data for {asset}")
    return {
        "schema_version": SCHEMA_VERSION,
        "asset": asset,
        "closes": prices,
        "highs": prices,   # market_chart has no OHLC; closes proxy for high/low
        "lows": prices,
        "last_price": prices[-1],
        "source": "coingecko-marketchart",
    }


# Adapter protocol expects `fetch`; expose fetch_bars under that name too.
async def fetch(asset: str = "BTC/USDT", timeframe: str = "1H", limit: int = 300) -> dict:
    return await fetch_bars(asset, timeframe, limit)

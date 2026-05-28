import httpx
import os

SCHEMA_VERSION = "1.0"

class SchemaError(Exception):
    pass

async def fetch(asset: str = "BTC/USDT") -> dict:
    # Map ccxt-style ticker to CoinGecko coin id
    _coingecko_ids = {
        "BTC/USDT": "bitcoin",
        "ETH/USDT": "ethereum",
        "SOL/USDT": "solana",
        "BNB/USDT": "binancecoin",
        "XRP/USDT": "ripple",
    }
    coin_id = _coingecko_ids.get(asset.upper(), asset.split("/")[0].lower())

    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": coin_id,
        "price_change_percentage": "24h",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    if not data:
        raise SchemaError(f"price adapter: no data returned for {asset} (coin_id={coin_id})")

    coin = data[0]
    required = {"current_price", "high_24h", "low_24h", "total_volume", "price_change_percentage_24h"}
    if not required.issubset(coin.keys()):
        raise SchemaError(f"price adapter schema mismatch: missing {required - coin.keys()}")

    return {
        "schema_version": SCHEMA_VERSION,
        "asset": asset,
        "price": float(coin["current_price"]),
        "high_24h": float(coin["high_24h"] or coin["current_price"]),
        "low_24h": float(coin["low_24h"] or coin["current_price"]),
        "volume_24h": float(coin["total_volume"] or 0),
        "change_pct_24h": float(coin["price_change_percentage_24h"] or 0),
    }

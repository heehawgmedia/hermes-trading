import httpx
import os

SCHEMA_VERSION = "1.0"

class SchemaError(Exception):
    pass

async def fetch(asset: str = "BTC/USDT") -> dict:
    symbol = asset.replace("/", "")
    api_key = os.getenv("EXCHANGE_API_KEY", "")
    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    required = {"lastPrice", "highPrice", "lowPrice", "volume", "priceChangePercent"}
    if not required.issubset(data.keys()):
        raise SchemaError(f"price adapter schema mismatch: missing {required - data.keys()}")
    return {
        "schema_version": SCHEMA_VERSION,
        "asset": asset,
        "price": float(data["lastPrice"]),
        "high_24h": float(data["highPrice"]),
        "low_24h": float(data["lowPrice"]),
        "volume_24h": float(data["volume"]),
        "change_pct_24h": float(data["priceChangePercent"]),
    }

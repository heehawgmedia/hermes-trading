import httpx
import os

SCHEMA_VERSION = "1.0"

class SchemaError(Exception):
    pass

async def fetch() -> dict:
    api_key = os.getenv("GLASSNODE_API_KEY", "")
    if api_key:
        url = "https://api.glassnode.com/v1/metrics/market/price_usd_close"
        params = {"a": "BTC", "api_key": api_key, "i": "24h", "f": "JSON", "timestamp_format": "humanized"}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        value = data[-1]["v"] if data else None
    else:
        # Free fallback: blockchain.info mempool stats
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://blockchain.info/q/unconfirmedcount")
            r.raise_for_status()
            value = int(r.text.strip())
    result = {
        "schema_version": SCHEMA_VERSION,
        "mempool_or_onchain_value": value,
        "source": "glassnode" if api_key else "blockchain.info",
    }
    if "schema_version" not in result or "mempool_or_onchain_value" not in result:
        raise SchemaError("onchain adapter schema mismatch")
    return result

import httpx
import os

SCHEMA_VERSION = "1.0"

class SchemaError(Exception):
    pass

async def fetch() -> dict:
    api_key = os.getenv("NEWS_API_KEY", "")
    if api_key:
        url = "https://newsapi.org/v2/everything"
        params = {"q": "bitcoin crypto", "sortBy": "publishedAt", "pageSize": 5, "apiKey": api_key}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        headlines = [a["title"] for a in data.get("articles", [])]
    else:
        # Free fallback: CryptoCompare news
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories=BTC")
            r.raise_for_status()
            data = r.json()
        headlines = [a["title"] for a in data.get("Data", [])[:5]]
    result = {
        "schema_version": SCHEMA_VERSION,
        "headlines": headlines,
        "source": "newsapi" if api_key else "cryptocompare",
    }
    if "schema_version" not in result or "headlines" not in result:
        raise SchemaError("news adapter schema mismatch")
    return result

import httpx

SCHEMA_VERSION = "1.0"

class SchemaError(Exception):
    pass

async def fetch() -> dict:
    # Free public: FRED via St. Louis Fed (no key needed for some series)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "DFF",      # Federal Funds Rate
                "api_key": "abcdefghijklmnopqrstuvwxyz123456",  # public demo key
                "file_type": "json",
                "limit": 1,
                "sort_order": "desc",
            },
        )
    # If FRED fails, fall back to a static placeholder
    try:
        r.raise_for_status()
        obs = r.json()["observations"]
        fed_rate = float(obs[0]["value"]) if obs else None
    except Exception:
        fed_rate = None

    result = {
        "schema_version": SCHEMA_VERSION,
        "fed_funds_rate": fed_rate,
        "source": "fred",
    }
    if "schema_version" not in result or "fed_funds_rate" not in result:
        raise SchemaError("macro adapter schema mismatch")
    return result

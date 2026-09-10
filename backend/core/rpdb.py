"""Personal RatingPosterDB credential validation."""

from urllib.parse import quote

import httpx

MAX_API_KEY_LENGTH = 255


def normalize_api_key(key: str | None) -> str | None:
    if key is None:
        return None
    key = key.strip()
    if len(key) > MAX_API_KEY_LENGTH:
        raise ValueError("RPDB API key must be at most 255 characters")
    return key or None


async def validate_api_key(key: str | None) -> bool:
    try:
        key = normalize_api_key(key)
        if not key:
            return False
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(
                f"https://api.ratingposterdb.com/{quote(key, safe='')}/isValid"
            )
            response.raise_for_status()
            data = response.json()
            return isinstance(data, dict) and data.get("valid") is True
    except (httpx.HTTPError, ValueError):
        return False

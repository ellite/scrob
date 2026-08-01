import base64
import zlib
from typing import Any

import httpx


DEFAULT_URL = "https://api.strem.io"
API_URL = f"{DEFAULT_URL}/api"
LINK_URL = "https://link.stremio.com/api/v2"
CINEMETA_URL = "https://v3-cinemeta.strem.io"
LIBRARY_COLLECTION = "libraryItem"
_TIMEOUT = 30.0


class StremioAPIError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


def _result(payload: Any, operation: str) -> Any:
    if not isinstance(payload, dict):
        raise StremioAPIError(f"Stremio {operation} returned an invalid response")
    error = payload.get("error")
    if error:
        if isinstance(error, dict):
            message = str(error.get("message") or "Unknown API error")
            code = error.get("code")
            try:
                parsed_code = int(code) if code is not None else None
            except (TypeError, ValueError):
                parsed_code = None
            raise StremioAPIError(f"Stremio {operation} failed: {message}", code=parsed_code)
        raise StremioAPIError(f"Stremio {operation} failed: {error}")
    if "result" not in payload:
        raise StremioAPIError(f"Stremio {operation} returned no result")
    return payload["result"]


async def _json(response: httpx.Response, operation: str) -> Any:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise StremioAPIError(
            f"Stremio {operation} failed ({response.status_code})"
        ) from exc
    try:
        return response.json()
    except ValueError as exc:
        raise StremioAPIError(
            f"Stremio {operation} returned invalid JSON"
        ) from exc


async def create_link_code() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        response = await client.get(f"{LINK_URL}/create", params={"type": "Create"})
    result = _result(await _json(response, "link creation"), "link creation")
    if not isinstance(result, dict) or not all(result.get(key) for key in ("code", "link", "qrcode")):
        raise StremioAPIError("Stremio link creation returned incomplete data")
    return result


async def read_link_code(code: str) -> str | None:
    normalized = code.strip().upper()
    if not normalized:
        raise StremioAPIError("Stremio link code is required")
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        response = await client.get(
            f"{LINK_URL}/read",
            params={"type": "Read", "code": normalized},
        )
    payload = await _json(response, "link authorization")
    try:
        result = _result(payload, "link authorization")
    except StremioAPIError as exc:
        if exc.code == 101:
            return None
        raise
    auth_key = result.get("authKey") if isinstance(result, dict) else None
    if not auth_key:
        raise StremioAPIError("Stremio link authorization returned no auth key")
    return str(auth_key)


async def _api_request(path: str, body: dict[str, Any], operation: str) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        response = await client.post(
            f"{API_URL}/{path}",
            headers={"Content-Type": "application/json"},
            json=body,
        )
    return _result(await _json(response, operation), operation)


async def get_user(auth_key: str) -> dict[str, Any]:
    result = await _api_request(
        "getUser",
        {"type": "GetUser", "authKey": auth_key},
        "account validation",
    )
    if not isinstance(result, dict) or not result.get("_id"):
        raise StremioAPIError("Stremio account validation returned incomplete data")
    return result


async def validate_auth_key(auth_key: str) -> dict[str, Any]:
    return await get_user(auth_key)


async def datastore_meta(auth_key: str) -> list[list[Any]]:
    result = await _api_request(
        "datastoreMeta",
        {"authKey": auth_key, "collection": LIBRARY_COLLECTION},
        "library metadata pull",
    )
    return result if isinstance(result, list) else []


async def datastore_get(
    auth_key: str,
    *,
    ids: list[str] | None = None,
    all_items: bool = False,
) -> list[dict[str, Any]]:
    result = await _api_request(
        "datastoreGet",
        {
            "authKey": auth_key,
            "collection": LIBRARY_COLLECTION,
            "ids": ids or [],
            "all": all_items,
        },
        "library pull",
    )
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


async def datastore_put(auth_key: str, changes: list[dict[str, Any]]) -> None:
    if not changes:
        return
    result = await _api_request(
        "datastorePut",
        {
            "authKey": auth_key,
            "collection": LIBRARY_COLLECTION,
            "changes": changes,
        },
        "library push",
    )
    if not isinstance(result, dict) or result.get("success") is not True:
        raise StremioAPIError("Stremio library push was not acknowledged")


async def logout(auth_key: str) -> None:
    await _api_request(
        "logout",
        {"type": "Logout", "authKey": auth_key},
        "logout",
    )


async def get_cinemeta_series(imdb_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        response = await client.get(f"{CINEMETA_URL}/meta/series/{imdb_id}.json")
    payload = await _json(response, "Cinemeta lookup")
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if not isinstance(meta, dict):
        raise StremioAPIError(f"Cinemeta series {imdb_id} was not found")
    return meta


def decode_watched_bitfield(serialized: str | None, video_ids: list[str]) -> set[str]:
    if not serialized or not video_ids:
        return set()
    try:
        anchor_video_id, anchor_length_raw, packed = serialized.rsplit(":", 2)
        anchor_length = int(anchor_length_raw)
        anchor_index = video_ids.index(anchor_video_id)
        previous = zlib.decompress(base64.b64decode(packed))
    except (ValueError, TypeError, zlib.error, base64.binascii.Error):
        return set()
    if anchor_length <= 0:
        return set()

    offset = (anchor_length - 1) - anchor_index
    watched: set[str] = set()
    for index, video_id in enumerate(video_ids):
        previous_index = index + offset
        if previous_index < 0 or previous_index >= anchor_length:
            continue
        byte_index, bit_index = divmod(previous_index, 8)
        if byte_index < len(previous) and previous[byte_index] & (1 << bit_index):
            watched.add(video_id)
    return watched


def encode_watched_bitfield(watched_ids: set[str], video_ids: list[str]) -> str | None:
    if not video_ids:
        return None
    values = bytearray((len(video_ids) + 7) // 8)
    last_watched_index = 0
    for index, video_id in enumerate(video_ids):
        if video_id in watched_ids:
            byte_index, bit_index = divmod(index, 8)
            values[byte_index] |= 1 << bit_index
            last_watched_index = index
    packed = base64.b64encode(zlib.compress(bytes(values))).decode("ascii")
    return f"{video_ids[last_watched_index]}:{last_watched_index + 1}:{packed}"

"""Lokal patch: externa betyg (Rotten Tomatoes Popcornmeter m.fl.) via MDBList."""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import mdblist as mdblist_client
from db import get_db
from dependencies import get_current_user
from models.users import User, UserSettings

router = APIRouter()

_TTL = 24 * 3600
_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}


async def _fetch(api_key: str, kind: str, tmdb_id: int) -> dict[str, Any]:
    key = (kind, tmdb_id)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        data = await mdblist_client._request_json(
            "GET", f"/tmdb/{kind}/{tmdb_id}", api_key, rate_limit_retries=0
        )
    except Exception:
        return {}
    out: dict[str, Any] = {}
    for r in (data or {}).get("ratings", []) if isinstance(data, dict) else []:
        src, val = r.get("source"), r.get("value")
        if src and val is not None:
            out[src] = val
    _cache[key] = (time.time(), out)
    return out


@router.get("/{type}/{tmdb_id}")
async def get_ext_ratings(
    type: str,
    tmdb_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kind = {"movie": "movie", "tv": "show", "show": "show"}.get(type)
    if not kind:
        raise HTTPException(status_code=400, detail="type must be movie or show")
    settings = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    ).scalar_one_or_none()
    api_key = getattr(settings, "mdblist_api_key", None)
    if not api_key:
        return {"popcorn": None, "tomatoes": None}
    r = await _fetch(api_key, kind, tmdb_id)
    return {"popcorn": r.get("popcorn"), "tomatoes": r.get("tomatoes")}

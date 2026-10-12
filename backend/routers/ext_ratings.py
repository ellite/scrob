"""Lokal patch: Rotten Tomatoes Popcornmeter via MDBList.

Betygen sparas i DATA_DIR/ext_ratings.json så att de överlever omstarter.
Färska i 24 h; misslyckas en uppdatering används det gamla värdet (upp till 30 dagar).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import mdblist as mdblist_client
from core.config import settings as app_settings
from db import get_db
from dependencies import get_current_user
from models.users import User, UserSettings

logger = logging.getLogger(__name__)
router = APIRouter()

_FRESH = 24 * 3600
_KEEP = 30 * 24 * 3600
_FILE = app_settings.data_dir / "ext_ratings.json"
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_paused_until = 0.0  # pausa MDBList-anrop en stund om dygnskvoten tar slut
_lock = asyncio.Lock()


def _load() -> None:
    try:
        raw = json.loads(_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as exc:  # trasig fil: börja om
        logger.warning("ext_ratings: kunde inte läsa %s: %s", _FILE, exc)
        return
    now = time.time()
    for k, v in raw.items():
        if isinstance(v, list) and len(v) == 2 and now - v[0] < _KEEP:
            _cache[k] = (float(v[0]), dict(v[1]))


def _save() -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: [t, d] for k, (t, d) in _cache.items()}), encoding="utf-8")
        os.replace(tmp, _FILE)
    except Exception as exc:
        logger.warning("ext_ratings: kunde inte spara %s: %s", _FILE, exc)


_load()


def _rt_url(entry: dict[str, Any]) -> str | None:
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return "https://www.rottentomatoes.com" + url
    return None


_HOT_MIN = 90        # Verified Hot: minst 90 % ...
_HOT_VOTES = 500     # ... och minst 500 verifierade röster (bred biopremiär)
_HOT_FROM_YEAR = 2019  # verifierade biljettbetyg finns sedan 2019; äldre röster är inte verifierade
_SCHEMA = 2


def _parse(data: Any, kind: str) -> dict[str, Any]:
    out: dict[str, Any] = {"v": _SCHEMA}
    if not isinstance(data, dict):
        return out
    year = data.get("year")
    if not year and isinstance(data.get("released"), str):
        year = data["released"][:4]
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    for r in data.get("ratings") or []:
        if not isinstance(r, dict):
            continue
        src = r.get("source")
        if src in ("popcorn", "tomatoesaudience") and r.get("value") is not None:
            out["popcorn"] = r.get("value")
            out["votes"] = r.get("votes")
            out["url"] = out.get("url") or _rt_url(r)
        elif src == "tomatoes":
            out["url"] = out.get("url") or _rt_url(r)
    try:
        p, votes = float(out.get("popcorn")), int(out.get("votes") or 0)
    except (TypeError, ValueError):
        p, votes = 0.0, 0
    # Uppskattning av RT:s Verified Hot. MDBList anger inte märket självt.
    out["hot"] = bool(
        kind == "movie" and p >= _HOT_MIN and votes >= _HOT_VOTES
        and year is not None and year >= _HOT_FROM_YEAR
    )
    return out


async def _fetch(api_key: str, kind: str, tmdb_id: int) -> dict[str, Any]:
    global _paused_until
    key = f"{kind}/{tmdb_id}"
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < _FRESH and hit[1].get("v") == _SCHEMA:
        return hit[1]
    stale = hit[1] if hit else {}
    if now < _paused_until:
        return stale
    try:
        data = await mdblist_client._request_json(
            "GET", f"/tmdb/{kind}/{tmdb_id}", api_key, rate_limit_retries=0
        )
    except mdblist_client.MDBListDailyLimitError:
        _paused_until = now + 3600
        return stale
    except Exception:
        return stale
    out = _parse(data, kind)
    async with _lock:
        _cache[key] = (now, out)
        _save()
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
        return {"popcorn": None, "url": None, "hot": False, "votes": None}
    r = await _fetch(api_key, kind, tmdb_id)
    return {"popcorn": r.get("popcorn"), "url": r.get("url"), "hot": bool(r.get("hot")), "votes": r.get("votes")}

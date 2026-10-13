"""Lokal patch: Rotten Tomatoes Popcornmeter via MDBList.

Betygen sparas i DATA_DIR/ext_ratings.json så att de överlever omstarter.
Färska i 24 h; misslyckas en uppdatering används det gamla värdet (upp till 30 dagar).
Filen skrivs högst var 10:e sekund, och samtidiga frågor om samma titel delar ett anrop.
"""
from __future__ import annotations

import asyncio
import atexit
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
_SAVE_DELAY = 10.0
_dirty = False
_save_task: asyncio.Task | None = None
_inflight: dict[str, asyncio.Future] = {}
_SCHEMA = 2


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
    global _dirty
    _dirty = False
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: [t, d] for k, (t, d) in _cache.items()}), encoding="utf-8")
        os.replace(tmp, _FILE)
    except Exception as exc:
        logger.warning("ext_ratings: kunde inte spara %s: %s", _FILE, exc)


def _save_if_dirty() -> None:
    if _dirty:
        _save()


async def _delayed_save() -> None:
    global _save_task
    try:
        await asyncio.sleep(_SAVE_DELAY)
        _save_if_dirty()
    finally:
        _save_task = None


def _schedule_save() -> None:
    """Markera ändrat och spara om en stund, så att 40 nya kort blir en skrivning."""
    global _dirty, _save_task
    _dirty = True
    if _save_task is None:
        _save_task = asyncio.get_running_loop().create_task(_delayed_save())


_load()
atexit.register(_save_if_dirty)  # spara det som inte hunnit skrivas vid avstängning


def _rt_url(entry: dict[str, Any]) -> str | None:
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return "https://www.rottentomatoes.com" + url
    return None


def _parse(data: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"v": _SCHEMA}
    if not isinstance(data, dict):
        return out
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
    return out


async def _fetch_remote(api_key: str, kind: str, tmdb_id: int, key: str, stale: dict[str, Any]) -> dict[str, Any]:
    global _paused_until
    now = time.time()
    try:
        data = await mdblist_client._request_json(
            "GET", f"/tmdb/{kind}/{tmdb_id}", api_key, rate_limit_retries=0
        )
    except mdblist_client.MDBListDailyLimitError:
        _paused_until = now + 3600
        return stale
    except Exception:
        return stale
    out = _parse(data)
    _cache[key] = (now, out)
    _schedule_save()
    return out


async def _fetch(api_key: str, kind: str, tmdb_id: int) -> dict[str, Any]:
    key = f"{kind}/{tmdb_id}"
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < _FRESH and hit[1].get("v") == _SCHEMA:
        return hit[1]
    stale = hit[1] if hit else {}
    if now < _paused_until:
        return stale
    # Samma titel efterfrågad samtidigt (t.ex. filmsidan och ett kort): ett anrop räcker.
    pending = _inflight.get(key)
    if pending is not None:
        return await asyncio.shield(pending)
    task = asyncio.get_running_loop().create_task(_fetch_remote(api_key, kind, tmdb_id, key, stale))
    _inflight[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _inflight.get(key) is task:
            _inflight.pop(key, None)


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
        return {"popcorn": None, "url": None, "votes": None}
    r = await _fetch(api_key, kind, tmdb_id)
    return {"popcorn": r.get("popcorn"), "url": r.get("url"), "votes": r.get("votes")}

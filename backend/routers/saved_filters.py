"""Per-user saved filter combinations for the Explore/collection filter panel."""

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from dependencies import get_current_user
from models.saved_filter import SavedFilter
from models.users import User

router = APIRouter()

MAX_PER_SCOPE = 50
MAX_KEYS = 40
MAX_VALUES = 300
_KEY_RE = re.compile(r"^[a-z_]{1,32}$")


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Name is required")
    if len(name) > 64:
        raise ValueError("Name must be 64 characters or fewer")
    return name


def _clean_params(params: dict[str, list[str]]) -> dict[str, list[str]]:
    cleaned: dict[str, list[str]] = {}
    total = 0
    for key, values in params.items():
        if not _KEY_RE.match(key):
            raise ValueError(f"Invalid filter key: {key}")
        vals = [str(v)[:64] for v in values if str(v).strip()]
        if vals:
            cleaned[key] = vals
            total += len(vals)
    if not cleaned:
        raise ValueError("Select at least one filter value to save")
    if len(cleaned) > MAX_KEYS or total > MAX_VALUES:
        raise ValueError("Too many filter values")
    return cleaned


class SavedFilterBody(BaseModel):
    scope: str
    name: str
    params: dict[str, list[str]]

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 128:
            raise ValueError("Invalid scope")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean_name(v)

    @field_validator("params")
    @classmethod
    def _params(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        return _clean_params(v)


class SavedFilterOverwrite(BaseModel):
    params: dict[str, list[str]]
    name: str | None = None  # optional rename while overwriting

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return _clean_name(v) if v is not None else None

    @field_validator("params")
    @classmethod
    def _params(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        return _clean_params(v)


def _out(f: SavedFilter) -> dict:
    return {"id": f.id, "scope": f.scope, "name": f.name, "params": f.params}


@router.get("")
async def list_saved_filters(
    scope: str = Query(..., max_length=128),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        select(SavedFilter)
        .where(SavedFilter.user_id == current_user.id, SavedFilter.scope == scope)
        .order_by(func.lower(SavedFilter.name))
    )).scalars().all()
    return [_out(f) for f in rows]


@router.post("", status_code=201)
async def create_saved_filter(
    body: SavedFilterBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = (await db.execute(
        select(func.count()).select_from(SavedFilter)
        .where(SavedFilter.user_id == current_user.id, SavedFilter.scope == body.scope)
    )).scalar_one()
    if count >= MAX_PER_SCOPE:
        raise HTTPException(status_code=400, detail=f"You can save at most {MAX_PER_SCOPE} filters per page")
    f = SavedFilter(user_id=current_user.id, scope=body.scope, name=body.name, params=body.params)
    db.add(f)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A saved filter with that name already exists")
    await db.refresh(f)
    return _out(f)


@router.put("/{filter_id}")
async def overwrite_saved_filter(
    filter_id: int,
    body: SavedFilterOverwrite,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    f = (await db.execute(
        select(SavedFilter).where(SavedFilter.id == filter_id, SavedFilter.user_id == current_user.id)
    )).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="Saved filter not found")
    f.params = body.params
    if body.name is not None:
        f.name = body.name
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A saved filter with that name already exists")
    await db.refresh(f)
    return _out(f)


@router.delete("/{filter_id}", status_code=204)
async def delete_saved_filter(
    filter_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    f = (await db.execute(
        select(SavedFilter).where(SavedFilter.id == filter_id, SavedFilter.user_id == current_user.id)
    )).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="Saved filter not found")
    await db.delete(f)
    await db.commit()

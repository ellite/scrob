from typing import Generator, Optional
from fastapi import Depends, Header, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from db import get_db
from models.users import User
from core.config import settings
from core.security import ALGORITHM
import schemas

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)

# Never assigned to a real row (Postgres identity columns start at 1) - read
# endpoints that accept an anonymous caller (logged-out navigation) pass this
# in place of a real user id to every existing user-scoped helper/query
# (enrich_with_state, get_user_tmdb_key, get_where_to_watch, ...) instead of
# threading `if current_user is not None` through each one individually.
# Every such query is a `WHERE user_id = :id` filter or an outer join keyed
# on it, so this id simply never matches anything and all personal state
# (watched, ratings, lists, collection) comes back empty/false, which is
# exactly the anonymous-safe result.
ANON_USER_ID = -1

async def get_current_user(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        if payload.get("type") == "2fa_pending":
            raise credentials_exception
        user_id: int = int(payload.get("sub"))
        if user_id is None:
            raise credentials_exception
        token_data = schemas.TokenPayload(sub=user_id)
    except (JWTError, ValueError):
        raise credentials_exception

    query = select(User).where(User.id == token_data.sub).options(selectinload(User.profile))
    result = await db.execute(query)
    user = result.scalar_one_or_none()
    
    if user is None:
        raise credentials_exception
    return user

async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


async def get_optional_user(
    db: AsyncSession = Depends(get_db),
    token: Optional[str] = Depends(oauth2_scheme_optional)
) -> Optional[User]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        if payload.get("type") == "2fa_pending":
            return None
        user_id_val = payload.get("sub")
        if user_id_val is None:
            return None
        user_id = int(user_id_val)
    except (JWTError, ValueError, TypeError):
        return None

    query = select(User).where(User.id == user_id).options(selectinload(User.profile))
    result = await db.execute(query)
    user = result.scalar_one_or_none()
    return user


async def get_current_user_or_api_key(
    db: AsyncSession = Depends(get_db),
    jwt_user: Optional[User] = Depends(get_optional_user),
    api_key: Optional[str] = Query(None, description="Scrob API key, as an alternative to a JWT Bearer token"),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
) -> User:
    """Same as get_current_user, but also accepts a Scrob API key (query param or
    X-Api-Key header) — the same key already used by webhooks and the Radarr/Sonarr
    compat endpoints — for callers that can't hold a JWT (e.g. external scripts)."""
    if jwt_user:
        return jwt_user

    key = api_key or x_api_key
    if key:
        result = await db.execute(select(User).where(User.api_key == key))
        user = result.scalar_one_or_none()
        if user:
            return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_optional_user_or_api_key(
    db: AsyncSession = Depends(get_db),
    jwt_user: Optional[User] = Depends(get_optional_user),
    api_key: Optional[str] = Query(None, description="Scrob API key, as an alternative to a JWT Bearer token"),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
) -> Optional[User]:
    """Same as get_current_user_or_api_key, but returns None instead of raising
    when neither a JWT nor an API key is present — for endpoints that fall back
    to treating the caller as an anonymous visitor rather than rejecting them
    outright (e.g. a list/profile page that may itself be public)."""
    if jwt_user:
        return jwt_user

    key = api_key or x_api_key
    if key:
        result = await db.execute(select(User).where(User.api_key == key))
        user = result.scalar_one_or_none()
        if user:
            return user

    return None

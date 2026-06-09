import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from sqlalchemy import select, func, delete as sa_delete
from sqlalchemy.exc import IntegrityError

from db import AsyncSessionLocal
from core.config import settings
from models.image_cache import ImageCache
from models.media import Media
from models.show import Show as ShowModel
from models.collection import Collection
from models.global_settings import GlobalSettings

logger = logging.getLogger(__name__)

# Valid TMDB image sizes to prevent traversal / arbitrary requests
ALLOWED_SIZES = {"w92", "w154", "w185", "w342", "w500", "w780", "w1280", "original"}

_last_prune_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
_PRUNE_INTERVAL = timedelta(minutes=5)


def parse_tmdb_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Parse TMDB URL or path into (size, path) tuple."""
    if not url:
        return None, None

    if "image.tmdb.org" in url:
        match = re.search(r"image\.tmdb\.org/t/p/([^/]+)(/.+)$", url)
        if match:
            return match.group(1), match.group(2)

    if url.startswith("/"):
        return "w500", url

    return None, None


async def download_and_cache_image(db, size: str, path: str, image_type: str = "ondemand") -> Optional[str]:
    """Download image from TMDB and cache it locally."""
    if size not in ALLOWED_SIZES:
        return None
    if ".." in path or not path.startswith("/"):
        return None

    stmt = select(ImageCache).where(ImageCache.path == path, ImageCache.size == size)
    cached = (await db.execute(stmt)).scalar_one_or_none()

    local_path = settings.data_dir / "image_cache" / size / path.lstrip("/")

    if cached:
        if local_path.exists():
            cached.last_accessed = datetime.now(timezone.utc)
            await db.commit()
            return str(local_path)
        else:
            await db.delete(cached)
            await db.commit()

    # Fetch from TMDB
    url = f"https://image.tmdb.org/t/p/{size}{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
            if r.status_code == 200:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(r.content)

                cache_entry = ImageCache(
                    path=path,
                    size=size,
                    image_type=image_type,
                    file_size=len(r.content),
                    last_accessed=datetime.now(timezone.utc),
                    created_at=datetime.now(timezone.utc),
                )
                try:
                    db.add(cache_entry)
                    await db.commit()
                except IntegrityError:
                    # Concurrent request already inserted this entry; update last_accessed instead
                    await db.rollback()
                    result = await db.execute(
                        select(ImageCache).where(ImageCache.path == path, ImageCache.size == size)
                    )
                    existing = result.scalar_one_or_none()
                    if existing:
                        existing.last_accessed = datetime.now(timezone.utc)
                        await db.commit()
                return str(local_path)
    except Exception as e:
        logger.error(f"Error downloading TMDB image {url}: {e}")

    return None


async def _prune_type(db, image_type: str, total_size: int, limit_bytes: int) -> int:
    """Delete oldest images of the given type until total_size is within limit. Returns updated total_size."""
    rows = (await db.execute(
        select(ImageCache.id, ImageCache.size, ImageCache.path, ImageCache.file_size)
        .where(ImageCache.image_type == image_type)
        .order_by(ImageCache.last_accessed.asc())
    )).all()

    ids_to_delete = []
    for row in rows:
        if total_size <= limit_bytes:
            break
        local_path = settings.data_dir / "image_cache" / row.size / row.path.lstrip("/")
        try:
            if local_path.exists():
                local_path.unlink()
        except Exception as e:
            logger.error(f"Failed to delete cached image file {local_path}: {e}")
        total_size -= row.file_size
        ids_to_delete.append(row.id)

    if ids_to_delete:
        await db.execute(sa_delete(ImageCache).where(ImageCache.id.in_(ids_to_delete)))

    return total_size


async def prune_cache(db, limit_gb: int):
    """Evict images from cache when exceeding the limit. Prunes on-demand first, then collected."""
    if not limit_gb or limit_gb <= 0:
        return

    limit_bytes = limit_gb * 1024 * 1024 * 1024

    total_size = (await db.execute(select(func.sum(ImageCache.file_size)))).scalar() or 0
    if total_size <= limit_bytes:
        return

    logger.info(f"Cache size ({total_size} bytes) exceeds limit ({limit_bytes} bytes). Pruning...")

    total_size = await _prune_type(db, "ondemand", total_size, limit_bytes)
    if total_size > limit_bytes:
        await _prune_type(db, "collected", total_size, limit_bytes)

    await db.commit()


async def prune_cache_bg(limit_gb: int):
    """Throttled background prune: runs at most once every 5 minutes."""
    global _last_prune_at
    if not limit_gb or limit_gb <= 0:
        return
    now = datetime.now(timezone.utc)
    if now - _last_prune_at < _PRUNE_INTERVAL:
        return
    _last_prune_at = now
    async with AsyncSessionLocal() as db:
        await prune_cache(db, limit_gb)


async def pre_cache_all_collected(db):
    """Find all image paths in user collections and pre-cache them."""
    urls_to_cache: set[str] = set()

    # Movies & Episodes — select only the columns we need
    for poster_path, backdrop_path in (await db.execute(
        select(Media.poster_path, Media.backdrop_path)
        .join(Collection, Collection.media_id == Media.id)
        .distinct()
    )).all():
        if poster_path:
            urls_to_cache.add(poster_path)
        if backdrop_path:
            urls_to_cache.add(backdrop_path)

    # Shows with collected episodes — select only the columns we need
    for poster_path, backdrop_path, tmdb_data in (await db.execute(
        select(ShowModel.poster_path, ShowModel.backdrop_path, ShowModel.tmdb_data)
        .join(Media, Media.show_id == ShowModel.id)
        .join(Collection, Collection.media_id == Media.id)
        .distinct()
    )).all():
        if poster_path:
            urls_to_cache.add(poster_path)
        if backdrop_path:
            urls_to_cache.add(backdrop_path)
        if tmdb_data and "seasons" in tmdb_data:
            for season in tmdb_data["seasons"]:
                sp = season.get("poster_path")
                if sp:
                    urls_to_cache.add(f"https://image.tmdb.org/t/p/w500{sp}")

    parsed_images = [
        (size, path)
        for url in urls_to_cache
        for size, path in [parse_tmdb_url(url)]
        if size and path
    ]

    if not parsed_images:
        return

    # Filter out already-cached entries
    cached_set: set[tuple[str, str]] = set((await db.execute(select(ImageCache.path, ImageCache.size))).all())
    to_download = [img for img in parsed_images if (img[1], img[0]) not in cached_set]

    if not to_download:
        return

    logger.info(f"Pre-caching {len(to_download)} collected images in the background...")

    for size, path in to_download:
        await download_and_cache_image(db, size, path, image_type="collected")
        await asyncio.sleep(0.1)  # Throttle to avoid hitting TMDB rate limits


async def pre_cache_all_collected_bg():
    """Run pre-caching in the background with a dedicated session."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(GlobalSettings.image_cache_enabled, GlobalSettings.image_cache_limit_gb)
            .where(GlobalSettings.id == 1)
        )).one_or_none()
        if not row or not row.image_cache_enabled:
            return
        await pre_cache_all_collected(db)
        if row.image_cache_limit_gb:
            await prune_cache(db, row.image_cache_limit_gb)


async def clear_image_cache(db):
    """Safely delete all local image cache files and clear database cache metadata."""
    import shutil

    await db.execute(sa_delete(ImageCache))
    await db.commit()

    cache_dir = settings.data_dir / "image_cache"
    if cache_dir.exists():
        try:
            shutil.rmtree(cache_dir)
        except Exception as e:
            logger.error(f"Failed to remove image cache directory: {e}")

    cache_dir.mkdir(parents=True, exist_ok=True)

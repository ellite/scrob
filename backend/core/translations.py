from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.media_translation import MediaTranslation
from models.show_translation import ShowTranslation
from models.profile import UserProfileData


async def get_user_metadata_language(db: AsyncSession, user_id: int) -> str | None:
    cache_key = f"metadata_lang_{user_id}"
    if cache_key not in db.info:
        result = await db.execute(
            select(UserProfileData.metadata_language).where(UserProfileData.user_id == user_id)
        )
        row = result.first()
        db.info[cache_key] = row[0] if row else None
    return db.info[cache_key]


async def get_media_translations(
    db: AsyncSession, media_ids: list[int], language: str
) -> dict[int, dict]:
    """Batch lookup — returns {media_id: {title, overview, tagline, poster_path}}."""
    if not media_ids or not language:
        return {}
    q = await db.execute(
        select(MediaTranslation).where(
            MediaTranslation.media_id.in_(media_ids),
            MediaTranslation.language == language,
        )
    )
    return {
        t.media_id: {
            "title": t.title,
            "overview": t.overview,
            "tagline": t.tagline,
            "poster_path": t.poster_path,
        }
        for t in q.scalars().all()
    }


async def get_show_translations(
    db: AsyncSession, show_ids: list[int], language: str
) -> dict[int, dict]:
    """Batch lookup — returns {show_id: {title, overview, tagline, poster_path}}."""
    if not show_ids or not language:
        return {}
    q = await db.execute(
        select(ShowTranslation).where(
            ShowTranslation.show_id.in_(show_ids),
            ShowTranslation.language == language,
        )
    )
    return {
        t.show_id: {
            "title": t.title,
            "overview": t.overview,
            "tagline": t.tagline,
            "poster_path": t.poster_path,
        }
        for t in q.scalars().all()
    }


async def upsert_media_translation(
    db: AsyncSession,
    media_id: int,
    language: str,
    title: str | None,
    overview: str | None,
    tagline: str | None = None,
    poster_path: str | None = None,
) -> None:
    stmt = (
        pg_insert(MediaTranslation)
        .values(
            media_id=media_id,
            language=language,
            title=title,
            overview=overview,
            tagline=tagline,
            poster_path=poster_path,
            fetched_at=datetime.utcnow(),
        )
        .on_conflict_do_update(
            index_elements=["media_id", "language"],
            set_={
                "title": title,
                "overview": overview,
                "tagline": tagline,
                "poster_path": poster_path,
                "fetched_at": datetime.utcnow(),
            },
        )
    )
    await db.execute(stmt)


async def upsert_show_translation(
    db: AsyncSession,
    show_id: int,
    language: str,
    title: str | None,
    overview: str | None,
    tagline: str | None = None,
    poster_path: str | None = None,
) -> None:
    stmt = (
        pg_insert(ShowTranslation)
        .values(
            show_id=show_id,
            language=language,
            title=title,
            overview=overview,
            tagline=tagline,
            poster_path=poster_path,
            fetched_at=datetime.utcnow(),
        )
        .on_conflict_do_update(
            index_elements=["show_id", "language"],
            set_={
                "title": title,
                "overview": overview,
                "tagline": tagline,
                "poster_path": poster_path,
                "fetched_at": datetime.utcnow(),
            },
        )
    )
    await db.execute(stmt)


def _translated_poster(raw: str | None) -> str | None:
    """Translations store a raw TMDB poster path (`/xxx.jpg`), but every
    endpoint that consumes these dicts returns full image URLs (media rows
    store the full URL). Expand the raw path so translated items don't come
    back with a bare path that breaks images."""
    from core import tmdb

    if raw and raw.startswith("/"):
        return tmdb.poster_url(raw)
    return raw


def apply_media_translations(items: list[dict], translations: dict[int, dict]) -> list[dict]:
    """Overlay stored translations onto format_media() / _format_media_item() dicts."""
    for item in items:
        t = translations.get(item.get("id"))
        if not t:
            continue
        if t.get("title"):
            item["title"] = t["title"]
        if t.get("overview"):
            item["overview"] = t["overview"]
        if t.get("tagline"):
            item["tagline"] = t["tagline"]
        if t.get("poster_path"):
            item["poster_path"] = _translated_poster(t["poster_path"])
    return items


def apply_show_translations(items: list[dict], translations: dict[int, dict]) -> list[dict]:
    """Overlay stored translations onto format_show() dicts."""
    for item in items:
        t = translations.get(item.get("id"))
        if not t:
            continue
        if t.get("title"):
            item["title"] = t["title"]
        if t.get("overview"):
            item["overview"] = t["overview"]
        if t.get("tagline"):
            item["tagline"] = t["tagline"]
        if t.get("poster_path"):
            item["poster_path"] = _translated_poster(t["poster_path"])
    return items

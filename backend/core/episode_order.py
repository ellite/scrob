import asyncio
import re
import unicodedata
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import tmdb, tvdb
from models.episode_order import EpisodeOrderMapping, UserShowEpisodeOrder


_VALID_ORDERS = {"tmdb", "tvdb"}


def _normalise_title(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _date_distance(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    try:
        return abs((date.fromisoformat(left) - date.fromisoformat(right)).days)
    except ValueError:
        return None


async def get_episode_order(
    db: AsyncSession,
    user_id: int,
    series_tmdb_id: int,
) -> UserShowEpisodeOrder | None:
    result = await db.execute(
        select(UserShowEpisodeOrder).where(
            UserShowEpisodeOrder.user_id == user_id,
            UserShowEpisodeOrder.series_tmdb_id == series_tmdb_id,
        )
    )
    return result.scalar_one_or_none()


async def get_mappings_for_tvdb_season(
    db: AsyncSession,
    series_tmdb_id: int,
    tvdb_season_number: int,
) -> list[EpisodeOrderMapping]:
    result = await db.execute(
        select(EpisodeOrderMapping)
        .where(
            EpisodeOrderMapping.series_tmdb_id == series_tmdb_id,
            EpisodeOrderMapping.tvdb_season_number == tvdb_season_number,
        )
        .order_by(EpisodeOrderMapping.tvdb_episode_number)
    )
    return list(result.scalars().all())


async def get_mapping_by_tvdb_position(
    db: AsyncSession,
    series_tmdb_id: int,
    tvdb_season_number: int,
    tvdb_episode_number: int,
) -> EpisodeOrderMapping | None:
    result = await db.execute(
        select(EpisodeOrderMapping).where(
            EpisodeOrderMapping.series_tmdb_id == series_tmdb_id,
            EpisodeOrderMapping.tvdb_season_number == tvdb_season_number,
            EpisodeOrderMapping.tvdb_episode_number == tvdb_episode_number,
        )
    )
    return result.scalar_one_or_none()


async def ensure_episode_order_mapping(
    db: AsyncSession,
    series_tmdb_id: int,
    tmdb_api_key: str,
    tvdb_api_key: str,
    *,
    force: bool = False,
) -> dict:
    existing_result = await db.execute(
        select(EpisodeOrderMapping).where(
            EpisodeOrderMapping.series_tmdb_id == series_tmdb_id
        )
    )
    existing = list(existing_result.scalars().all())
    show_data = await tmdb.get_show(series_tmdb_id, api_key=tmdb_api_key)
    tvdb_id = (show_data.get("external_ids") or {}).get("tvdb_id")
    if not tvdb_id:
        raise ValueError("TMDB does not expose a TVDB identifier for this show")
    tvdb_id = int(tvdb_id)

    if existing and not force:
        return {
            "tvdb_id": tvdb_id,
            "matched": len(existing),
            "tmdb_episodes": len(existing),
            "unmatched": 0,
        }

    tmdb_season_numbers = sorted(
        {
            int(season["season_number"])
            for season in show_data.get("seasons") or []
            if season.get("season_number") is not None
        }
    )
    tvdb_series = await tvdb.get_series(tvdb_id, tvdb_api_key)
    tvdb_season_numbers = sorted(
        {
            int(season.get("number"))
            for season in tvdb_series.get("seasons") or []
            if season.get("number") is not None
            and (season.get("type") or {}).get("type") in (None, "official")
        }
    )
    if not tvdb_season_numbers:
        tvdb_season_numbers = sorted(
            {
                int(episode.get("seasonNumber"))
                for episode in tvdb_series.get("episodes") or []
                if episode.get("seasonNumber") is not None
            }
        )

    tmdb_seasons, tvdb_seasons = await asyncio.gather(
        asyncio.gather(
            *(tmdb.get_season(series_tmdb_id, number, api_key=tmdb_api_key) for number in tmdb_season_numbers)
        ),
        asyncio.gather(
            *(tvdb.get_series_episodes(tvdb_id, number, tvdb_api_key) for number in tvdb_season_numbers)
        ),
    )

    tmdb_episodes = [
        episode
        for season in tmdb_seasons
        for episode in season.get("episodes") or []
        if episode.get("season_number") is not None and episode.get("episode_number") is not None
    ]
    tvdb_episodes = [episode for season in tvdb_seasons for episode in season]
    tvdb_by_id = {
        int(episode["id"]): episode
        for episode in tvdb_episodes
        if episode.get("id") is not None
    }

    semaphore = asyncio.Semaphore(5)

    async def load_external_ids(episode: dict) -> tuple[dict, dict]:
        async with semaphore:
            ids = await tmdb.get_episode_external_ids(
                series_tmdb_id,
                int(episode["season_number"]),
                int(episode["episode_number"]),
                api_key=tmdb_api_key,
            )
        return episode, ids

    external_rows = await asyncio.gather(
        *(load_external_ids(episode) for episode in tmdb_episodes)
    )

    used_tvdb_ids: set[int] = set()
    mappings: list[EpisodeOrderMapping] = []
    unmatched: list[dict] = []

    for episode, external_ids in external_rows:
        external_tvdb_id = external_ids.get("tvdb_id")
        match = tvdb_by_id.get(int(external_tvdb_id)) if external_tvdb_id else None
        method = "external_id"
        if match is None:
            title = _normalise_title(episode.get("name"))
            candidates = [
                candidate
                for candidate in tvdb_episodes
                if candidate.get("id") not in used_tvdb_ids
                and title
                and _normalise_title(candidate.get("name")) == title
                and (_date_distance(episode.get("air_date"), candidate.get("aired")) or 0) <= 1
            ]
            if len(candidates) == 1:
                match = candidates[0]
                method = "title_date"

        if match is None or match.get("seasonNumber") is None or match.get("number") is None:
            unmatched.append(episode)
            continue

        mapped_tvdb_id = int(match["id"])
        used_tvdb_ids.add(mapped_tvdb_id)
        mappings.append(
            EpisodeOrderMapping(
                series_tmdb_id=series_tmdb_id,
                tmdb_season_number=int(episode["season_number"]),
                tmdb_episode_number=int(episode["episode_number"]),
                tmdb_episode_id=int(episode["id"]),
                tvdb_id=mapped_tvdb_id,
                tvdb_season_number=int(match["seasonNumber"]),
                tvdb_episode_number=int(match["number"]),
                match_method=method,
            )
        )

    if not mappings:
        raise ValueError("No TMDB episodes could be matched to TVDB")

    await db.execute(
        delete(EpisodeOrderMapping).where(
            EpisodeOrderMapping.series_tmdb_id == series_tmdb_id
        )
    )
    db.add_all(mappings)
    await db.flush()

    return {
        "tvdb_id": tvdb_id,
        "matched": len(mappings),
        "tmdb_episodes": len(tmdb_episodes),
        "unmatched": len(unmatched),
    }


def validate_episode_order(value: str) -> str:
    if value not in _VALID_ORDERS:
        raise ValueError(f"Unsupported episode order: {value}")
    return value

"""Live regression test for GitHub #436 (Test A - see test_436_tvdb_push_fallback.py
for the full end-to-end version through _run_full_push).

Proves core.jellyfin.build_tvdb_index and find_episode_in_series actually
work against a real Jellyfin server: before the fix, Scrob's matching code
only ever looked at ProviderIds.Tmdb (core/jellyfin.py's
get_jellyfin_tmdb_id/build_tmdb_index), so a show Scrob only knew via TVDB
was unmatchable even though Jellyfin's own TheTVDB plugin tags it with a
Tvdb provider id. This doesn't touch Scrob's DB at all - it calls the new
core.jellyfin functions directly against the dev fixture's "John Doe" show.
"""
import asyncio

import httpx

from core import jellyfin

JOHN_DOE_TVDB_ID = 78839


def test_build_tvdb_index_and_find_episode_in_series(jellyfin_conn: dict):
    async def _run():
        index = await jellyfin.build_tvdb_index(jellyfin_conn["url"], jellyfin_conn["token"], "Series")
        assert JOHN_DOE_TVDB_ID in index, (
            f"expected the dev fixture's John Doe (Tvdb {JOHN_DOE_TVDB_ID}) in the TVDB index, got keys {list(index)}"
        )
        series_id = index[JOHN_DOE_TVDB_ID]

        episode = await jellyfin.find_episode_in_series(
            jellyfin_conn["url"], jellyfin_conn["token"], series_id, season=1, episode=1,
            user_id=jellyfin_conn["server_user_id"],
        )
        assert episode is not None, "find_episode_in_series found nothing for John Doe S1E1 via the TVDB-resolved series id"
        assert episode["Name"] == "Pilot"

    asyncio.run(_run())

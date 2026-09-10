import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.base import MediaType
from models.episode_order import ShowEpisodePosition
from routers import media as media_router
from routers.media import _attach_episode_order_fields, _resolve_add_overrides, RequestOverrides


def _pos(sid, canon_season, canon_episode, display_season, display_episode, order_key="tvdb:dvd"):
    return ShowEpisodePosition(
        series_tmdb_id=sid, order_key=order_key,
        display_season=display_season, display_episode=display_episode,
        tmdb_episode_id=1,
        tmdb_season_number=canon_season, tmdb_episode_number=canon_episode,
    )


class AttachEpisodeOrderFieldsTests(unittest.TestCase):
    """#174: every surface renders a show's episodes in the ordering the user
    picked for it - _attach_episode_order_fields attaches the order key plus
    display_season_number/display_episode_number from ShowEpisodePosition."""

    def test_episode_gets_display_position(self) -> None:
        item = {"type": "episode", "show_tmdb_id": 100, "season_number": 4, "episode_number": 12}
        _attach_episode_order_fields(
            item, {100: "tvdb:dvd"}, {(100, 4, 12): _pos(100, 4, 12, 3, 8)},
        )
        self.assertEqual(item["show_episode_order"], "tvdb:dvd")
        self.assertEqual(item["display_season_number"], 3)
        self.assertEqual(item["display_episode_number"], 8)

    def test_tvdb_sourced_episode_never_gets_a_translated_position(self) -> None:
        item = {
            "type": "episode", "show_tmdb_id": 100,
            "season_number": 4, "episode_number": 12, "tvdb_sourced": True,
        }
        _attach_episode_order_fields(
            item, {100: "tvdb:dvd"}, {(100, 4, 12): _pos(100, 4, 12, 9, 99)},
        )
        self.assertEqual(item["show_episode_order"], "tvdb:dvd")
        self.assertNotIn("display_season_number", item)
        self.assertNotIn("display_episode_number", item)

    def test_episode_with_no_positions_only_sets_key(self) -> None:
        item = {"type": "episode", "show_tmdb_id": 100, "season_number": 4, "episode_number": 12}
        _attach_episode_order_fields(item, {100: "tmdb:group:abc"}, {})
        self.assertEqual(item["show_episode_order"], "tmdb:group:abc")
        self.assertNotIn("display_season_number", item)

    def test_aired_key_is_a_noop(self) -> None:
        item = {"type": "episode", "show_tmdb_id": 100, "season_number": 4, "episode_number": 12}
        _attach_episode_order_fields(
            item, {100: "tmdb:aired"}, {(100, 4, 12): _pos(100, 4, 12, 3, 8)},
        )
        self.assertNotIn("show_episode_order", item)
        self.assertNotIn("display_season_number", item)

    def test_episode_with_no_key_is_a_noop(self) -> None:
        item = {"type": "episode", "show_tmdb_id": 100, "season_number": 4, "episode_number": 12}
        _attach_episode_order_fields(item, {}, {})
        self.assertNotIn("show_episode_order", item)

    def test_season_list_item_picks_lowest_episode_numbers_display_season(self) -> None:
        item = {"type": "series", "tmdb_id": 100, "season_number": 4}
        _attach_episode_order_fields(item, {100: "tvdb:dvd"}, {
            (100, 4, 5): _pos(100, 4, 5, 3, 1),
            (100, 4, 1): _pos(100, 4, 1, 3, 9),
            (100, 5, 1): _pos(100, 5, 1, 4, 1),
        })
        self.assertEqual(item["show_episode_order"], "tvdb:dvd")
        # Lowest canonical episode in season 4 is episode 1 -> display season 3.
        self.assertEqual(item["display_season_number"], 3)
        self.assertNotIn("display_episode_number", item)

    def test_whole_show_item_sets_key_without_season_fields(self) -> None:
        item = {"type": "series", "tmdb_id": 100}
        _attach_episode_order_fields(item, {100: "tvdb:dvd"}, {(100, 4, 12): _pos(100, 4, 12, 3, 8)})
        self.assertEqual(item["show_episode_order"], "tvdb:dvd")
        self.assertNotIn("display_season_number", item)

    def test_movie_item_is_a_noop(self) -> None:
        item = {"type": "movie", "tmdb_id": 550}
        _attach_episode_order_fields(item, {550: "tvdb:dvd"}, {})
        self.assertNotIn("show_episode_order", item)
class ResolveAddOverridesTests(unittest.TestCase):
    """The customize-on-add popup lets an admin override the root folder/
    quality profile/tags/season-folder for a single Radarr/Sonarr add - this
    must only ever apply for an admin, regardless of the *_customize_on_add
    settings (those only control whether the frontend shows the picker)."""

    def test_admin_overrides_are_honored(self) -> None:
        overrides = RequestOverrides(root_folder="/movies-4k")
        self.assertIs(_resolve_add_overrides(overrides, True), overrides)

    def test_non_admin_overrides_are_ignored(self) -> None:
        overrides = RequestOverrides(root_folder="/movies-4k")
        self.assertIsNone(_resolve_add_overrides(overrides, False))

    def test_no_overrides_sent_is_a_noop_for_an_admin(self) -> None:
        self.assertIsNone(_resolve_add_overrides(None, True))

    def test_no_overrides_sent_is_a_noop_for_a_non_admin(self) -> None:
        self.assertIsNone(_resolve_add_overrides(None, False))


class _FakeExecResult:
    def all(self):
        return []

    def scalars(self):
        return self

    def first(self):
        return None


class TmdbListStudioParamsTests(unittest.IsolatedAsyncioTestCase):
    """/media/tmdb/list with network=/company= drives the /network/{id} and
    /studio/{id} browse pages: network is TV-only and the vote-count floor is
    dropped so a whole back-catalogue shows."""

    async def _call(self, **kwargs):
        base = dict(
            type=MediaType.movie, category="popular", page=1, genre=[], year=[],
            min_rating=None, status=None, original_language=None, network=None,
            company=None, collection=[], watch=[], arr=[], in_list=[],
            db=SimpleNamespace(execute=AsyncMock(return_value=_FakeExecResult())),
            current_user=SimpleNamespace(id=7),
        )
        base.update(kwargs)
        discover_shows = AsyncMock(return_value={"results": [], "page": 1, "total_pages": 1, "total_results": 0})
        discover_movies = AsyncMock(return_value={"results": [], "page": 1, "total_pages": 1, "total_results": 0})
        with patch.object(media_router, "get_user_tmdb_key", AsyncMock(return_value="k")), \
             patch.object(media_router, "check_tmdb_key", lambda _k: True), \
             patch.object(media_router, "get_user_metadata_language", AsyncMock(return_value=None)), \
             patch.object(media_router, "enrich_with_state", AsyncMock(side_effect=lambda db, uid, items: items)), \
             patch.object(media_router.tmdb, "discover_shows", discover_shows), \
             patch.object(media_router.tmdb, "discover_movies", discover_movies):
            await media_router.get_tmdb_list(**base)
        return discover_shows, discover_movies

    async def test_network_forces_series_and_zero_vote_floor(self) -> None:
        shows, movies = await self._call(type=MediaType.movie, network=9)
        movies.assert_not_called()
        shows.assert_awaited_once()
        kw = shows.await_args.kwargs
        self.assertEqual(kw["with_networks"], 9)
        self.assertEqual(kw["vote_count_min"], 0)

    async def test_company_passes_with_companies_to_movies(self) -> None:
        shows, movies = await self._call(type=MediaType.movie, company=1957)
        movies.assert_awaited_once()
        kw = movies.await_args.kwargs
        self.assertEqual(kw["with_companies"], 1957)
        self.assertEqual(kw["vote_count_min"], 0)

    async def test_plain_list_keeps_default_vote_floor(self) -> None:
        shows, movies = await self._call(type=MediaType.series, genre=["Drama"])
        shows.assert_awaited_once()
        self.assertIsNone(shows.await_args.kwargs["vote_count_min"])
        self.assertIsNone(shows.await_args.kwargs["with_networks"])


if __name__ == "__main__":
    unittest.main()

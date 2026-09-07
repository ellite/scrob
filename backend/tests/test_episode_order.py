import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from core.episode_order import (
    ORDER_AIRED,
    _match_tmdb_to_tvdb_episodes,
    _merge_episode_media,
    _reset_season_mapping_guards,
    ensure_episode_order_mapping,
    get_default_episode_order,
    ensure_episode_order_mapping_for_season,
    get_episode_orders_for_series,
    get_tmdb_to_tvdb_positions,
    reconcile_divergent_episode_media,
    validate_episode_order,
)
from models.base import MediaType
from models.collection import Collection
from models.comments import Comment
from models.episode_order import EpisodeOrderMapping, UserShowEpisodeOrder
from models.events import WatchEvent
from models.lists import ListItem
from models.media import Media
from models.playback_progress import PlaybackProgress
from models.ratings import Rating
from models.rewatch import RewatchProgress
from models.show import Show as ShowModel
from models.sync import SyncStatus
from routers.ratings import RatingIn, submit_rating
from routers.shows import (
    ApplyDefaultEpisodeOrderRequest,
    _enrich_tvdb_seasons,
    _run_default_episode_order,
    _run_episode_order_mapping,
    apply_default_episode_order,
    apply_default_episode_order_to_new_shows,
    get_tvdb_season,
)


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _ExistingResult(_EmptyResult):
    def __init__(self, items):
        self.items = items

    def all(self):
        return self.items


class _ScalarOneResult:
    def __init__(self, item):
        self.item = item

    def scalar_one_or_none(self):
        return self.item

    def scalars(self):
        return self

    def first(self):
        return self.item


class _NestedTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False  # let exceptions propagate, like a real SAVEPOINT rollback


class EpisodeOrderMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_bidirectional_positions_from_external_ids_and_safe_fallback(self) -> None:
        db = AsyncMock()
        db.execute.return_value = _EmptyResult()
        db.add_all = MagicMock()

        tmdb_show = {
            "external_ids": {"tvdb_id": 389597},
            "seasons": [{"season_number": 1}],
        }
        tmdb_season = {
            "episodes": [
                {
                    "id": 5876034,
                    "season_number": 1,
                    "episode_number": 13,
                    "name": "You Aren't E-Rank, Are You?",
                    "air_date": "2025-01-04",
                },
                {
                    "id": 5876035,
                    "season_number": 1,
                    "episode_number": 14,
                    "name": "Éveil",
                    "air_date": "2025-01-11",
                },
            ]
        }
        tvdb_show = {"seasons": [{"number": 2, "type": {"type": "official"}}]}
        tvdb_episodes = [
            {
                "id": 10414110,
                "seasonNumber": 2,
                "number": 1,
                "name": "You Aren't E-Rank, Are You?",
                "aired": "2025-01-05",
            },
            {
                "id": 10414111,
                "seasonNumber": 2,
                "number": 2,
                "name": "Eveil",
                "aired": "2025-01-12",
            },
        ]

        with (
            patch("core.episode_order.tmdb.get_show", AsyncMock(return_value=tmdb_show)),
            patch("core.episode_order.tmdb.get_season", AsyncMock(return_value=tmdb_season)),
            patch(
                "core.episode_order.tmdb.get_episode_external_ids",
                AsyncMock(side_effect=[{"tvdb_id": 10414110}, {"tvdb_id": None}]),
            ),
            patch("core.episode_order.tvdb.get_series", AsyncMock(return_value=tvdb_show)),
            patch(
                "core.episode_order.tvdb.get_series_episodes",
                AsyncMock(return_value=tvdb_episodes),
            ),
        ):
            summary = await ensure_episode_order_mapping(
                db,
                127532,
                "tmdb-key",
                "tvdb-key",
            )

        self.assertEqual(
            summary,
            {"tvdb_id": 389597, "matched": 2, "tmdb_episodes": 2, "unmatched": 0},
        )
        mappings = db.add_all.call_args.args[0]
        self.assertEqual(
            [
                (
                    mapping.tmdb_season_number,
                    mapping.tmdb_episode_number,
                    mapping.tvdb_season_number,
                    mapping.tvdb_episode_number,
                    mapping.match_method,
                )
                for mapping in mappings
            ],
            [
                (1, 13, 2, 1, "external_id"),
                (1, 14, 2, 2, "title_date"),
            ],
        )

    async def test_cached_mapping_keeps_the_series_tvdb_id(self) -> None:
        db = AsyncMock()
        db.execute.return_value = _ExistingResult([
            SimpleNamespace(tvdb_id=10414110),
        ])
        with patch(
            "core.episode_order.tmdb.get_show",
            AsyncMock(return_value={"external_ids": {"tvdb_id": 389597}}),
        ):
            summary = await ensure_episode_order_mapping(
                db,
                127532,
                "tmdb-key",
                "tvdb-key",
            )

        self.assertEqual(summary["tvdb_id"], 389597)
        self.assertEqual(summary["matched"], 1)

    async def test_force_bypasses_the_shared_tmdb_and_tvdb_caches(self) -> None:
        # Regression for: a "Refresh Metadata"/force-recompute call must not
        # silently return whatever TMDB/TVDB response is already sitting in
        # the shared cache from an unrelated request moments earlier.
        db = AsyncMock()
        db.execute.return_value = _ExistingResult([SimpleNamespace(tvdb_id=10414110)])
        db.add_all = MagicMock()

        tmdb_show = {"external_ids": {"tvdb_id": 389597}, "seasons": [{"season_number": 1}]}
        tmdb_season = {
            "episodes": [{"id": 1, "season_number": 1, "episode_number": 1, "name": "Pilot", "air_date": "2025-01-01"}]
        }
        tvdb_show = {"seasons": [{"number": 1, "type": {"type": "official"}}]}
        tvdb_episodes = [{"id": 100, "seasonNumber": 1, "number": 1, "name": "Pilot", "aired": "2025-01-01"}]

        get_show = AsyncMock(return_value=tmdb_show)
        get_season = AsyncMock(return_value=tmdb_season)
        get_tvdb_series = AsyncMock(return_value=tvdb_show)
        get_tvdb_episodes = AsyncMock(return_value=tvdb_episodes)
        with (
            patch("core.episode_order.tmdb.get_show", get_show),
            patch("core.episode_order.tmdb.get_season", get_season),
            patch("core.episode_order.tmdb.get_episode_external_ids", AsyncMock(return_value={"tvdb_id": None})),
            patch("core.episode_order.tvdb.get_series", get_tvdb_series),
            patch("core.episode_order.tvdb.get_series_episodes", get_tvdb_episodes),
        ):
            await ensure_episode_order_mapping(db, 127532, "tmdb-key", "tvdb-key", force=True)

        self.assertIsNone(get_show.call_args.kwargs["cache_ttl"])
        self.assertIsNone(get_season.call_args.kwargs["cache_ttl"])
        self.assertIsNone(get_tvdb_series.call_args.kwargs["cache_ttl"])
        self.assertIsNone(get_tvdb_episodes.call_args.kwargs["cache_ttl"])

    async def test_tvdb_episodes_are_fetched_in_english_for_title_matching(self) -> None:
        # #351: TVDB defaults to each episode's original-language title, so an
        # anime's TVDB titles were Japanese against TMDB's English ones and
        # nothing matched.
        db = AsyncMock()
        db.execute.return_value = _ExistingResult([SimpleNamespace(tvdb_id=10414110)])
        db.add_all = MagicMock()

        get_tvdb_episodes = AsyncMock(
            return_value=[{"id": 100, "seasonNumber": 1, "number": 1, "name": "Pilot", "aired": "2025-01-01"}]
        )
        with (
            patch(
                "core.episode_order.tmdb.get_show",
                AsyncMock(return_value={"external_ids": {"tvdb_id": 389597}, "seasons": [{"season_number": 1}]}),
            ),
            patch(
                "core.episode_order.tmdb.get_season",
                AsyncMock(return_value={"episodes": [
                    {"id": 1, "season_number": 1, "episode_number": 1, "name": "Pilot", "air_date": "2025-01-01"}
                ]}),
            ),
            patch("core.episode_order.tmdb.get_episode_external_ids", AsyncMock(return_value={"tvdb_id": None})),
            patch(
                "core.episode_order.tvdb.get_series",
                AsyncMock(return_value={"seasons": [{"number": 1, "type": {"type": "official"}}]}),
            ),
            patch("core.episode_order.tvdb.get_series_episodes", get_tvdb_episodes),
        ):
            await ensure_episode_order_mapping(db, 127532, "tmdb-key", "tvdb-key", force=True)

        self.assertEqual(get_tvdb_episodes.call_args.kwargs["language"], "eng")

    async def test_without_force_the_shared_tmdb_cache_is_used(self) -> None:
        # An already-mapped show short-circuits before any tvdb.* call, so
        # only tmdb.get_show's cache behavior is observable on this path.
        db = AsyncMock()
        db.execute.return_value = _ExistingResult([SimpleNamespace(tvdb_id=10414110)])

        get_show = AsyncMock(return_value={"external_ids": {"tvdb_id": 389597}})
        with patch("core.episode_order.tmdb.get_show", get_show):
            await ensure_episode_order_mapping(db, 127532, "tmdb-key", "tvdb-key")

        self.assertIsNotNone(get_show.call_args.kwargs["cache_ttl"])

    async def test_tvdb_season_rating_stays_local(self) -> None:
        media = Media(
            id=1,
            tmdb_id=127532,
            media_type=MediaType.series,
            title="Solo Leveling",
        )
        rating = Rating(
            id=2,
            user_id=3,
            media_id=1,
            season_number=2,
            episode_order="tvdb",
            rating=8.0,
            rated_at=datetime(2026, 7, 19),
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    _ScalarOneResult(media),
                    _ScalarOneResult(rating),
                ]
            ),
            add=MagicMock(),
            commit=AsyncMock(),
            refresh=AsyncMock(),
        )

        with patch(
            "routers.sync._fan_out_changes_to_other_connections",
            AsyncMock(),
        ) as fan_out:
            result = await submit_rating(
                RatingIn(
                    tmdb_id=127532,
                    media_type="series",
                    rating=8.0,
                    season_number=2,
                    episode_order="tvdb",
                ),
                db=db,
                current_user=SimpleNamespace(id=3),
            )

        fan_out.assert_not_awaited()
        self.assertEqual(result["season_number"], 2)
        self.assertEqual(result["episode_order"], "tvdb")

    async def test_tvdb_season_metadata_uses_tvdb_text_and_mapped_tmdb_rating(self) -> None:
        mapping = SimpleNamespace(
            tvdb_season_number=2,
            tmdb_season_number=1,
        )
        tvdb_season = {
            "id": 2120511,
            "number": 2,
            "name": "-Arise from the Shadow-",
            "image": "https://artworks.thetvdb.com/season-2.jpg",
            "translations": {
                "nameTranslations": [
                    {"language": "fra", "name": "Arise from the Shadow"},
                ],
                "overviewTranslations": [
                    {"language": "eng", "overview": "English overview"},
                    {"language": "fra", "overview": "Résumé français"},
                ],
            },
        }
        tmdb_show = {
            "seasons": [
                {
                    "season_number": 1,
                    "vote_average": 8.7,
                    "overview": "TMDB overview",
                },
            ],
        }

        with (
            patch(
                "routers.shows.tvdb_client.get_season",
                AsyncMock(return_value=tvdb_season),
            ),
            patch(
                "routers.shows.tmdb.get_show",
                AsyncMock(return_value=tmdb_show),
            ),
        ):
            seasons, _ = await _enrich_tvdb_seasons(
                [{
                    "id": 2120511,
                    "season_number": 2,
                    "name": "Season 2",
                    "overview": None,
                    "poster_path": None,
                    "episode_count": 13,
                    "air_date": "2025-01-05",
                }],
                [mapping],
                tvdb_api_key="tvdb-key",
                tvdb_language="fra",
                series_tmdb_id=127532,
                tmdb_api_key="tmdb-key",
                metadata_language="fr",
            )

        self.assertEqual(seasons[0]["name"], "Arise from the Shadow")
        self.assertEqual(seasons[0]["overview"], "Résumé français")
        self.assertEqual(seasons[0]["tmdb_rating"], 8.7)
        self.assertEqual(seasons[0]["episode_count"], 13)

    async def test_tvdb_season_counts_unmapped_episode_via_raw_position_fallback(self) -> None:
        """Regression test: an episode with no explicit TMDB<->TVDB mapping must
        still be counted toward watched/collected state if a local episode
        exists at the same raw (season, episode) position, instead of being
        silently dropped from the season's counts."""
        show = ShowModel(id=42, tvdb_id=500, tmdb_id=999)
        mapping = SimpleNamespace(
            tvdb_id=5001,
            tvdb_season_number=2,
            tvdb_episode_number=1,
            tmdb_season_number=1,
            tmdb_episode_number=1,
            tmdb_episode_id=9001,
        )
        mapped_episode = Media(
            id=201,
            show_id=42,
            media_type=MediaType.episode,
            season_number=1,
            episode_number=1,
            tmdb_id=9001,
        )
        # No mapping targets this episode, but it shares its raw TMDB position
        # (season 2, episode 2) with the unmapped TVDB episode below.
        unmapped_episode = Media(
            id=202,
            show_id=42,
            media_type=MediaType.episode,
            season_number=2,
            episode_number=2,
            tmdb_id=9002,
        )

        raw_series = {
            "id": 500,
            "name": "Test Show",
            "remoteIds": [{"sourceName": "TheMovieDB", "id": "999"}],
            "seasons": [{"number": 2, "type": {"type": "official"}, "id": 111}],
        }
        raw_episodes = [
            {"id": 5001, "seasonNumber": 2, "number": 1, "name": "Ep1", "aired": "2025-01-01"},
            {"id": 5002, "seasonNumber": 2, "number": 2, "name": "Ep2", "aired": "2025-01-08"},
        ]

        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    _ScalarOneResult(show),                             # show_result
                    _ExistingResult([mapping]),                         # mapping_result
                    _ExistingResult([mapped_episode, unmapped_episode]),  # ep_result
                    _ScalarOneResult(None),                             # get_active_rewatch — none active
                    _ExistingResult([(201,), (202,)]),                  # watched_q — both watched
                    _ExistingResult([(201,)]),                          # collected_q — only 201 collected
                    _ExistingResult([]),                                # episode_ratings_q
                    # find_media (core/identity.py) tries tmdb_id, then falls back to tvdb_id
                    _ScalarOneResult(None),                             # show media by tmdb_id
                    _ScalarOneResult(None),                             # show media by tvdb_id
                    _ExistingResult([]),                                # user_lists_q — no lists for this user
                    _ScalarOneResult(None),                             # whole-show rating media by tmdb_id
                    _ScalarOneResult(None),                             # whole-show rating media by tvdb_id
                ]
            ),
        )

        with (
            patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value="tvdb-key")),
            patch("routers.shows.get_user_metadata_language", AsyncMock(return_value=None)),
            patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value=None)),
            patch("routers.shows.tvdb_client.get_series", AsyncMock(return_value=raw_series)),
            patch("routers.shows.tvdb_client.get_series_episodes", AsyncMock(return_value=raw_episodes)),
            patch("routers.shows.tvdb_client.get_season", AsyncMock(return_value={})),
        ):
            result = await get_tvdb_season(
                tvdb_id=500,
                season_number=2,
                db=db,
                current_user=SimpleNamespace(id=7),
            )

        episodes_by_number = {ep["episode_number"]: ep for ep in result["episodes"]}
        self.assertEqual(episodes_by_number[1]["id"], 201)
        self.assertTrue(episodes_by_number[1]["watched"])
        self.assertTrue(episodes_by_number[1]["in_library"])

        self.assertEqual(episodes_by_number[2]["id"], 202)
        self.assertTrue(episodes_by_number[2]["watched"])
        self.assertFalse(episodes_by_number[2]["in_library"])

        self.assertEqual(result["season_watch_pct"], 100)
        self.assertTrue(result["season_watched"])
        self.assertEqual(result["season_collection_pct"], 50)

    def test_rejects_unknown_episode_order(self) -> None:
        # Legacy values normalise to the new key grammar (#174 compat shim).
        self.assertEqual(validate_episode_order("tmdb"), "tmdb:aired")
        self.assertEqual(validate_episode_order("tvdb"), "tvdb:official")
        self.assertEqual(validate_episode_order("tvdb:dvd"), "tvdb:dvd")
        self.assertEqual(validate_episode_order("tmdb:group:12345"), "tmdb:group:12345")
        with self.assertRaisesRegex(ValueError, "Unsupported episode order"):
            validate_episode_order("absolute")
        with self.assertRaisesRegex(ValueError, "Unsupported episode order"):
            validate_episode_order("tvdb:bogus")


class EnsureEpisodeOrderMappingForSeasonTests(unittest.IsolatedAsyncioTestCase):
    """#162: on-demand, per-season mapping resolution for webhook ingest -
    additive (never deletes existing rows for other seasons) and cheap
    (skips per-episode external-id lookups for episodes already mapped)."""

    async def asyncSetUp(self) -> None:
        # The per-show lock / recent-attempt guards are module state (#412).
        _reset_season_mapping_guards()

    def _show(self):
        return SimpleNamespace(tmdb_id=127532, tvdb_id=389597)

    def _db(self, existing=None):
        db = AsyncMock()
        db.execute.return_value = _ExistingResult(existing or [])
        db.add_all = MagicMock()
        db.begin_nested = MagicMock(return_value=_NestedTxn())
        return db

    def _one_matchable_episode(self):
        """Patches for a show whose single unmapped TMDB episode matches one
        TVDB episode by title + air date. Returns (patch context, get_show mock)."""
        tmdb_show = {"seasons": [{"season_number": 2}]}
        tmdb_season_2 = {"episodes": [
            {"id": 2, "season_number": 2, "episode_number": 1, "name": "Ep2", "air_date": "2025-02-01"},
        ]}
        tvdb_show = {"seasons": [{"number": 3, "type": {"type": "official"}}]}
        tvdb_episodes = [
            {"id": 700, "seasonNumber": 3, "number": 1, "name": "Ep2", "aired": "2025-02-02"},
        ]
        get_show = AsyncMock(return_value=tmdb_show)
        ctx = (
            patch("core.episode_order.tmdb.get_show", get_show),
            patch("core.episode_order.tmdb.get_season", AsyncMock(return_value=tmdb_season_2)),
            patch("core.episode_order.tmdb.get_episode_external_ids", AsyncMock(return_value={"tvdb_id": None})),
            patch("core.episode_order.tvdb.get_series", AsyncMock(return_value=tvdb_show)),
            patch("core.episode_order.tvdb.get_series_episodes", AsyncMock(return_value=tvdb_episodes)),
        )
        return ctx, get_show

    async def test_returns_empty_without_tvdb_id_or_api_keys(self) -> None:
        db = AsyncMock()
        show_no_tvdb = SimpleNamespace(tmdb_id=127532, tvdb_id=None)
        self.assertEqual(
            await ensure_episode_order_mapping_for_season(db, show_no_tvdb, 2, "tmdb-key", "tvdb-key"),
            [],
        )
        self.assertEqual(
            await ensure_episode_order_mapping_for_season(db, self._show(), 2, None, "tvdb-key"),
            [],
        )
        self.assertEqual(
            await ensure_episode_order_mapping_for_season(db, self._show(), 2, "tmdb-key", None),
            [],
        )
        db.execute.assert_not_awaited()

    async def test_returns_empty_when_season_already_has_a_mapping(self) -> None:
        db = AsyncMock()
        db.execute.return_value = _ExistingResult([
            SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=13, tvdb_season_number=2, tvdb_id=999),
        ])
        result = await ensure_episode_order_mapping_for_season(db, self._show(), 2, "tmdb-key", "tvdb-key")
        self.assertEqual(result, [])

    async def test_additively_inserts_only_unmapped_episodes_for_a_new_season(self) -> None:
        # Season 1 (TMDB) already mapped to TVDB season 1 - a webhook now
        # reports TVDB season 2, which must be resolved without touching the
        # existing season 1 mapping.
        existing_mapping = SimpleNamespace(
            tmdb_season_number=1, tmdb_episode_number=1, tvdb_season_number=1, tvdb_id=500,
        )
        db = self._db([existing_mapping])

        tmdb_show = {"seasons": [{"season_number": 1}, {"season_number": 2}]}
        tmdb_season_1 = {"episodes": [
            {"id": 1, "season_number": 1, "episode_number": 1, "name": "Ep1", "air_date": "2025-01-01"},
        ]}
        tmdb_season_2 = {"episodes": [
            {"id": 2, "season_number": 2, "episode_number": 1, "name": "Ep2", "air_date": "2025-02-01"},
        ]}

        def _get_season(series_tmdb_id, number, **kwargs):
            return {1: tmdb_season_1, 2: tmdb_season_2}[number]

        tvdb_show = {"seasons": [{"number": 3, "type": {"type": "official"}}]}
        tvdb_episodes = [
            {"id": 700, "seasonNumber": 3, "number": 1, "name": "Ep2", "aired": "2025-02-02"},
        ]

        with (
            patch("core.episode_order.tmdb.get_show", AsyncMock(return_value=tmdb_show)),
            patch("core.episode_order.tmdb.get_season", AsyncMock(side_effect=_get_season)),
            patch(
                "core.episode_order.tmdb.get_episode_external_ids",
                AsyncMock(return_value={"tvdb_id": None}),
            ),
            patch("core.episode_order.tvdb.get_series", AsyncMock(return_value=tvdb_show)),
            patch(
                "core.episode_order.tvdb.get_series_episodes",
                AsyncMock(return_value=tvdb_episodes),
            ),
        ):
            result = await ensure_episode_order_mapping_for_season(
                db, self._show(), 3, "tmdb-key", "tvdb-key"
            )

        # Only the season-2/season-3 pair was newly matched - season 1 was
        # never re-matched (no external-id lookup would have found season 1
        # a candidate here anyway, since it's excluded from the input set).
        self.assertEqual(len(result), 1)
        self.assertEqual(
            (result[0].tmdb_season_number, result[0].tmdb_episode_number,
             result[0].tvdb_season_number, result[0].tvdb_episode_number),
            (2, 1, 3, 1),
        )
        # Additive: no delete() of existing rows, just an insert of the new one,
        # inside a savepoint (see test_insert_conflict_returns_empty_and_keeps_the_session_usable).
        db.add_all.assert_called_once()
        db.begin_nested.assert_called_once()

    async def test_fetch_failure_returns_empty_instead_of_raising(self) -> None:
        db = AsyncMock()
        db.execute.return_value = _EmptyResult()
        with patch("core.episode_order.tmdb.get_show", AsyncMock(side_effect=RuntimeError("boom"))):
            result = await ensure_episode_order_mapping_for_season(
                db, self._show(), 2, "tmdb-key", "tvdb-key"
            )
        self.assertEqual(result, [])

    # -- #412: a huge show whose mapping can't be persisted must not be
    #    recomputed on every one of Jellyfin's per-second progress webhooks.

    async def test_insert_conflict_returns_empty_and_keeps_the_session_usable(self) -> None:
        db = self._db()
        db.flush = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("duplicate key")))
        ctx, _ = self._one_matchable_episode()
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4]:
            result = await ensure_episode_order_mapping_for_season(
                db, self._show(), 3, "tmdb-key", "tvdb-key"
            )
        # Swallowed, not raised: the webhook that called us must not 500.
        self.assertEqual(result, [])
        # ...and the INSERT ran inside a SAVEPOINT so only it was rolled back.
        db.begin_nested.assert_called_once()
        db.add_all.assert_called_once()

    async def test_one_attempt_per_show_per_ttl_whatever_the_outcome(self) -> None:
        db = self._db()
        ctx, get_show = self._one_matchable_episode()
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4]:
            first = await ensure_episode_order_mapping_for_season(db, self._show(), 3, "tmdb-key", "tvdb-key")
            second = await ensure_episode_order_mapping_for_season(db, self._show(), 3, "tmdb-key", "tvdb-key")
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        # The second call never went back to TMDB (nor to the DB).
        get_show.assert_awaited_once()
        self.assertEqual(db.execute.await_count, 1)

        # A failed attempt counts too - that's the whole point (#412: the
        # show's batch could never be inserted, and each retry cost ~4 400
        # TMDB requests).
        _reset_season_mapping_guards()
        db = self._db()
        db.execute.return_value = _EmptyResult()
        boom = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("core.episode_order.tmdb.get_show", boom):
            await ensure_episode_order_mapping_for_season(db, self._show(), 2, "tmdb-key", "tvdb-key")
            await ensure_episode_order_mapping_for_season(db, self._show(), 2, "tmdb-key", "tvdb-key")
        boom.assert_awaited_once()

    async def test_attempt_ttl_expires(self) -> None:
        db = self._db()
        ctx, get_show = self._one_matchable_episode()
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4]:
            await ensure_episode_order_mapping_for_season(db, self._show(), 3, "tmdb-key", "tvdb-key")
            # Patch the module's `time` reference, not time.monotonic itself -
            # the event loop clock runs on the latter.
            with patch("core.episode_order.time", SimpleNamespace(monotonic=lambda: 1e12)):
                await ensure_episode_order_mapping_for_season(db, self._show(), 3, "tmdb-key", "tvdb-key")
        self.assertEqual(get_show.await_count, 2)

    async def test_already_mapped_season_does_not_block_another_season(self) -> None:
        # The cheap "this season already has rows" early return isn't an
        # attempt: a webhook for a genuinely unmapped season right after it
        # must still get resolved.
        mapped = SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=1, tvdb_season_number=1, tvdb_id=500)
        db = self._db([mapped])
        ctx, get_show = self._one_matchable_episode()
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4]:
            self.assertEqual(
                await ensure_episode_order_mapping_for_season(db, self._show(), 1, "tmdb-key", "tvdb-key"), [],
            )
            result = await ensure_episode_order_mapping_for_season(db, self._show(), 3, "tmdb-key", "tvdb-key")
        self.assertEqual(len(result), 1)
        get_show.assert_awaited_once()

    async def test_concurrent_call_for_the_same_show_returns_empty_instead_of_waiting(self) -> None:
        db = self._db()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_get_show(*args, **kwargs):
            started.set()
            await release.wait()
            return {"seasons": [{"season_number": 2}]}

        ctx, _ = self._one_matchable_episode()
        with patch("core.episode_order.tmdb.get_show", slow_get_show), ctx[1], ctx[2], ctx[3], ctx[4]:
            first = asyncio.create_task(
                ensure_episode_order_mapping_for_season(db, self._show(), 3, "tmdb-key", "tvdb-key")
            )
            await started.wait()
            # Same show, while the first computation is still fetching: no
            # waiting (that would park this request's DB connection for the
            # whole computation), no second fetch - just "no translation".
            second = await asyncio.wait_for(
                ensure_episode_order_mapping_for_season(db, self._show(), 3, "tmdb-key", "tvdb-key"),
                timeout=1,
            )
            self.assertEqual(second, [])
            # A different show is unaffected.
            other = SimpleNamespace(tmdb_id=999, tvdb_id=888)
            other_db = self._db([SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=1, tvdb_season_number=3, tvdb_id=1)])
            self.assertEqual(
                await ensure_episode_order_mapping_for_season(other_db, other, 3, "tmdb-key", "tvdb-key"), [],
            )
            other_db.execute.assert_awaited_once()
            release.set()
            self.assertEqual(len(await first), 1)


class MatchTmdbToTvdbEpisodesTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_tvdb_episode_backs_at_most_one_mapping(self) -> None:
        # TMDB pointing two of its episodes at the same TVDB episode (seen
        # on The Daily Show, #412) must not produce two rows with the same
        # (series, tvdb_id) - the unique constraint would reject the whole
        # batch and nothing would ever be persisted for the show.
        tmdb_episodes = [
            {"id": 1, "season_number": 0, "episode_number": 31, "name": "Special", "air_date": "2012-09-24"},
            {"id": 2, "season_number": 18, "episode_number": 1, "name": "Monday", "air_date": "2012-09-24"},
            {"id": 3, "season_number": 18, "episode_number": 2, "name": "Tuesday", "air_date": "2012-09-25"},
        ]
        tvdb_episodes = [
            {"id": 4404874, "seasonNumber": 18, "number": 1, "name": "Special", "aired": "2012-09-24"},
            {"id": 4404875, "seasonNumber": 18, "number": 2, "name": "Tuesday", "aired": "2012-09-25"},
        ]

        async def external_ids(series_tmdb_id, season, episode, api_key=None):
            # Episodes 1 and 2 both carry TVDB id 4404874; episode 3 has none.
            return {"tvdb_id": 4404874 if (season, episode) in ((0, 31), (18, 1)) else None}

        with patch("core.episode_order.tmdb.get_episode_external_ids", external_ids):
            mappings = await _match_tmdb_to_tvdb_episodes(tmdb_episodes, tvdb_episodes, 2224, "tmdb-key")

        by_tmdb = {(m.tmdb_season_number, m.tmdb_episode_number): m for m in mappings}
        self.assertEqual(set(by_tmdb), {(0, 31), (18, 2)})
        self.assertEqual(by_tmdb[(0, 31)].tvdb_id, 4404874)
        self.assertEqual(by_tmdb[(0, 31)].match_method, "external_id")
        self.assertEqual(by_tmdb[(18, 2)].tvdb_id, 4404875)
        self.assertEqual(len({m.tvdb_id for m in mappings}), len(mappings))

    async def test_used_tvdb_ids_seed_applies_to_external_id_matches_too(self) -> None:
        tmdb_episodes = [
            {"id": 1, "season_number": 1, "episode_number": 1, "name": "Pilot", "air_date": "2020-01-01"},
        ]
        tvdb_episodes = [
            {"id": 10, "seasonNumber": 1, "number": 1, "name": "Pilot", "aired": "2020-01-01"},
        ]
        with patch("core.episode_order.tmdb.get_episode_external_ids", AsyncMock(return_value={"tvdb_id": 10})):
            mappings = await _match_tmdb_to_tvdb_episodes(
                tmdb_episodes, tvdb_episodes, 1, "tmdb-key", used_tvdb_ids={10},
            )
        # Already mapped to another TMDB episode in an earlier (incremental)
        # pass - not available, by either matching method.
        self.assertEqual(mappings, [])


class MergeEpisodeMediaTests(unittest.IsolatedAsyncioTestCase):
    """#162: _merge_episode_media moves every reference from a divergent
    (TVDB-numbered) episode Media row onto the canonical (TMDB-numbered) one,
    deduplicating instead of moving wherever that would violate a table's
    real uniqueness (one row per user/list/rewatch-cycle)."""

    def setUp(self):
        self.canonical = Media(id=1, tmdb_id=100, media_type=MediaType.episode, title="Ep", season_number=1, episode_number=25, show_id=9)
        self.divergent = Media(id=2, tmdb_id=200, media_type=MediaType.episode, title="Ep", season_number=2, episode_number=1, show_id=9)

    def _db(self, side_effect):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=side_effect)
        db.delete = AsyncMock()
        db.flush = AsyncMock()
        return db

    async def test_watch_events_are_bulk_reassigned_and_divergent_row_deleted(self) -> None:
        db = self._db([
            None,  # update(WatchEvent)
            _ExistingResult([]),  # RewatchProgress divergent
            _ExistingResult([]),  # PlaybackProgress divergent
            _ExistingResult([]),  # Rating divergent
            _ExistingResult([]),  # ListItem divergent
            _ExistingResult([]),  # Collection divergent
            None,  # update(Comment)
        ])
        await _merge_episode_media(db, self.canonical, self.divergent)
        db.delete.assert_awaited_once_with(self.divergent)
        db.flush.assert_awaited()

    async def test_rewatch_progress_moves_or_dedupes_per_rewatch_cycle(self) -> None:
        moves = RewatchProgress(id=10, rewatch_id=1, media_id=self.divergent.id)
        clashes = RewatchProgress(id=11, rewatch_id=2, media_id=self.divergent.id)
        db = self._db([
            None,
            _ExistingResult([moves, clashes]),
            _ScalarOneResult(None),  # no canonical row for rewatch_id=1 - moves
            _ScalarOneResult(RewatchProgress(id=99, rewatch_id=2, media_id=self.canonical.id)),  # clash
            _ExistingResult([]), _ExistingResult([]), _ExistingResult([]), _ExistingResult([]),
            None,
        ])
        await _merge_episode_media(db, self.canonical, self.divergent)
        self.assertEqual(moves.media_id, self.canonical.id)
        db.delete.assert_any_call(clashes)

    async def test_playback_progress_moves_or_dedupes_per_user(self) -> None:
        moves = PlaybackProgress(id=10, user_id=1, media_id=self.divergent.id)
        clashes = PlaybackProgress(id=11, user_id=2, media_id=self.divergent.id)
        db = self._db([
            None,
            _ExistingResult([]),
            _ExistingResult([moves, clashes]),
            _ScalarOneResult(None),  # user 1 has no canonical progress - moves
            _ScalarOneResult(PlaybackProgress(id=99, user_id=2, media_id=self.canonical.id)),  # clash
            _ExistingResult([]), _ExistingResult([]), _ExistingResult([]),
            None,
        ])
        await _merge_episode_media(db, self.canonical, self.divergent)
        self.assertEqual(moves.media_id, self.canonical.id)
        db.delete.assert_any_call(clashes)

    async def test_rating_moves_or_dedupes_per_user(self) -> None:
        moves = Rating(id=10, user_id=1, media_id=self.divergent.id, rating=8.0)
        clashes = Rating(id=11, user_id=2, media_id=self.divergent.id, rating=6.0)
        db = self._db([
            None,
            _ExistingResult([]), _ExistingResult([]),
            _ExistingResult([moves, clashes]),
            _ScalarOneResult(None),  # user 1 has no canonical rating - moves
            _ScalarOneResult(Rating(id=99, user_id=2, media_id=self.canonical.id, rating=9.0)),  # clash
            _ExistingResult([]), _ExistingResult([]),
            None,
        ])
        await _merge_episode_media(db, self.canonical, self.divergent)
        self.assertEqual(moves.media_id, self.canonical.id)
        db.delete.assert_any_call(clashes)

    async def test_list_item_moves_or_dedupes_per_list(self) -> None:
        moves = ListItem(id=10, list_id=1, media_id=self.divergent.id)
        clashes = ListItem(id=11, list_id=2, media_id=self.divergent.id)
        db = self._db([
            None,
            _ExistingResult([]), _ExistingResult([]), _ExistingResult([]),
            _ExistingResult([moves, clashes]),
            _ScalarOneResult(None),  # list 1 has no canonical item - moves
            _ScalarOneResult(ListItem(id=99, list_id=2, media_id=self.canonical.id)),  # clash
            _ExistingResult([]),
            None,
        ])
        await _merge_episode_media(db, self.canonical, self.divergent)
        self.assertEqual(moves.media_id, self.canonical.id)
        db.delete.assert_any_call(clashes)

    async def test_collection_moves_or_dedupes_per_user(self) -> None:
        moves = Collection(id=10, user_id=1, media_id=self.divergent.id)
        clashes = Collection(id=11, user_id=2, media_id=self.divergent.id)
        db = self._db([
            None,
            _ExistingResult([]), _ExistingResult([]), _ExistingResult([]), _ExistingResult([]),
            _ExistingResult([moves, clashes]),
            _ScalarOneResult(None),  # user 1 has no canonical collection entry - moves
            _ScalarOneResult(Collection(id=99, user_id=2, media_id=self.canonical.id)),  # clash
            None,
        ])
        await _merge_episode_media(db, self.canonical, self.divergent)
        self.assertEqual(moves.media_id, self.canonical.id)
        db.delete.assert_any_call(clashes)


class ReconcileDivergentEpisodeMediaTests(unittest.IsolatedAsyncioTestCase):
    def _show(self):
        return SimpleNamespace(id=9, tmdb_id=100, tvdb_id=389597)

    async def test_no_op_when_positions_already_match(self) -> None:
        db = AsyncMock()
        db.execute.return_value = _ExistingResult([
            SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=1, tvdb_season_number=1, tvdb_episode_number=1),
        ])
        stats = await reconcile_divergent_episode_media(db, self._show())
        self.assertEqual(stats, {"merged": 0, "checked": 0})

    async def test_no_op_when_only_one_side_of_the_pair_exists(self) -> None:
        canonical = Media(id=1, tmdb_id=100, media_type=MediaType.episode, show_id=9, season_number=1, episode_number=25)
        db = AsyncMock()
        db.execute.side_effect = [
            _ExistingResult([SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=25, tvdb_season_number=2, tvdb_episode_number=1)]),
            _ScalarOneResult(canonical),  # canonical found
            _ScalarOneResult(None),  # divergent not found - nothing to merge
        ]
        stats = await reconcile_divergent_episode_media(db, self._show())
        self.assertEqual(stats["merged"], 0)

    async def test_merges_a_genuinely_divergent_pair(self) -> None:
        canonical = Media(id=1, tmdb_id=100, media_type=MediaType.episode, show_id=9, season_number=1, episode_number=25)
        # tmdb_id=700 matches the mapping's tvdb_id below - this is exactly
        # what enrich_episode_from_tvdb stores for the TVDB-fallback-created
        # artifact of this mapped episode (core/enrichment.py).
        divergent = Media(id=2, tmdb_id=700, media_type=MediaType.episode, show_id=9, season_number=2, episode_number=1)
        db = AsyncMock()
        db.begin_nested = MagicMock(return_value=_NestedTxn())
        db.delete = AsyncMock()
        db.flush = AsyncMock()
        db.execute.side_effect = [
            _ExistingResult([SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=25, tvdb_season_number=2, tvdb_episode_number=1, tvdb_id=700)]),
            _ScalarOneResult(canonical),
            _ScalarOneResult(divergent),
            None,  # update(WatchEvent)
            _ExistingResult([]), _ExistingResult([]), _ExistingResult([]), _ExistingResult([]), _ExistingResult([]),
            None,  # update(Comment)
        ]
        stats = await reconcile_divergent_episode_media(db, self._show())
        self.assertEqual(stats, {"merged": 1, "checked": 1})
        db.delete.assert_awaited_once_with(divergent)

    async def test_merges_a_divergent_row_identified_by_tvdb_id(self) -> None:
        # #447: since the dual-identity change the TVDB episode id lives in
        # Media.tvdb_id and tmdb_id is NULL for a TVDB-only episode.
        canonical = Media(id=1, tmdb_id=100, media_type=MediaType.episode, show_id=9, season_number=1, episode_number=25)
        divergent = Media(id=2, tmdb_id=None, tvdb_id=700, media_type=MediaType.episode, show_id=9, season_number=2, episode_number=1)
        db = AsyncMock()
        db.begin_nested = MagicMock(return_value=_NestedTxn())
        db.delete = AsyncMock()
        db.flush = AsyncMock()
        db.execute.side_effect = [
            _ExistingResult([SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=25, tvdb_season_number=2, tvdb_episode_number=1, tvdb_id=700)]),
            _ScalarOneResult(canonical),
            _ScalarOneResult(divergent),
            None,  # update(WatchEvent)
            _ExistingResult([]), _ExistingResult([]), _ExistingResult([]), _ExistingResult([]), _ExistingResult([]),
            None,  # update(Comment)
        ]
        stats = await reconcile_divergent_episode_media(db, self._show())
        self.assertEqual(stats, {"merged": 1, "checked": 1})
        db.delete.assert_awaited_once_with(divergent)

    async def test_does_not_merge_when_tvdb_id_differs_even_if_tmdb_id_collides(self) -> None:
        # A row with its own, different tvdb_id is a different episode; a
        # tmdb_id that happens to equal the mapping's tvdb_id must not count.
        canonical = Media(id=1, tmdb_id=100, media_type=MediaType.episode, show_id=9, season_number=1, episode_number=25)
        other = Media(id=3, tmdb_id=700, tvdb_id=555, media_type=MediaType.episode, show_id=9, season_number=2, episode_number=1)
        db = AsyncMock()
        db.begin_nested = MagicMock(return_value=_NestedTxn())
        db.execute.side_effect = [
            _ExistingResult([SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=25, tvdb_season_number=2, tvdb_episode_number=1, tvdb_id=700)]),
            _ScalarOneResult(canonical),
            _ScalarOneResult(other),
        ]
        stats = await reconcile_divergent_episode_media(db, self._show())
        self.assertEqual(stats["merged"], 0)
        db.begin_nested.assert_not_called()

    async def test_does_not_merge_an_unrelated_episode_at_the_same_raw_position(self) -> None:
        # Regression: TVDB and TMDB can assign different, unrelated episodes
        # to the same numeric (season, episode) slot. A Media row sitting at
        # the mapping's raw TVDB position is only safe to merge if it's
        # provably the TVDB-fallback artifact for THIS episode (tmdb_id ==
        # mapping.tvdb_id) - otherwise it's a real, different episode that
        # happens to share the same numbers, and merging it would corrupt
        # both episodes' watch history/ratings/etc.
        canonical = Media(id=1, tmdb_id=100, media_type=MediaType.episode, show_id=9, season_number=1, episode_number=25)
        unrelated_real_episode = Media(id=3, tmdb_id=999, media_type=MediaType.episode, show_id=9, season_number=2, episode_number=1)
        db = AsyncMock()
        db.begin_nested = MagicMock(return_value=_NestedTxn())
        db.delete = AsyncMock()
        db.execute.side_effect = [
            _ExistingResult([SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=25, tvdb_season_number=2, tvdb_episode_number=1, tvdb_id=700)]),
            _ScalarOneResult(canonical),
            _ScalarOneResult(unrelated_real_episode),
        ]
        stats = await reconcile_divergent_episode_media(db, self._show())
        self.assertEqual(stats["merged"], 0)
        db.begin_nested.assert_not_called()
        db.delete.assert_not_awaited()

    async def test_scoped_to_one_tvdb_season_when_given(self) -> None:
        db = AsyncMock()
        db.execute.return_value = _ExistingResult([])
        await reconcile_divergent_episode_media(db, self._show(), season_number=3)
        stmt = db.execute.call_args.args[0]
        self.assertIn("tvdb_season_number", str(stmt))

    async def test_one_bad_pair_does_not_abort_the_rest(self) -> None:
        # begin_nested raising must be caught and logged, not propagated -
        # this always runs as a side effect of a webhook or mapping job.
        db = AsyncMock()
        db.begin_nested = MagicMock(side_effect=RuntimeError("boom"))
        db.execute.side_effect = [
            _ExistingResult([SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=25, tvdb_season_number=2, tvdb_episode_number=1, tvdb_id=700)]),
            _ScalarOneResult(Media(id=1, tmdb_id=100, media_type=MediaType.episode, show_id=9, season_number=1, episode_number=25)),
            _ScalarOneResult(Media(id=2, tmdb_id=700, media_type=MediaType.episode, show_id=9, season_number=2, episode_number=1)),
        ]
        stats = await reconcile_divergent_episode_media(db, self._show())
        self.assertEqual(stats, {"merged": 0, "checked": 1})


class RunEpisodeOrderMappingReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """#162: after a full mapping recompute (switching a show to TVDB
    ordering, or Refresh Metadata), the show's divergent episode Media rows
    must get reconciled too - not just newly-mapped episodes going forward."""

    async def test_reconcile_called_with_local_show_after_tvdb_order_built(self) -> None:
        local_show = SimpleNamespace(id=9, tmdb_id=127532, tvdb_id=None)

        fake_db = SimpleNamespace(
            execute=AsyncMock(return_value=_ScalarOneResult(local_show)),
            commit=AsyncMock(),
            add=MagicMock(),
        )

        class _FakeSessionCtx:
            async def __aenter__(self):
                return fake_db

            async def __aexit__(self, *exc):
                return False

        async def _build(*a, **kw):
            # build_order_positions resolves + sets the tvdb id on the show.
            local_show.tvdb_id = 389597
            return {"order_key": "tvdb:official", "positions": 12}

        reconcile_mock = AsyncMock()
        with (
            patch("routers.shows.async_sessionmaker", MagicMock(return_value=lambda: _FakeSessionCtx())),
            patch("routers.shows.build_order_positions", AsyncMock(side_effect=_build)),
            patch("routers.shows.get_episode_order", AsyncMock(return_value=None)),
            patch("routers.shows.reconcile_divergent_episode_media", reconcile_mock),
        ):
            await _run_episode_order_mapping(1, 55, 127532, "tvdb:official", "TVDB Aired Order", "tmdb-key", "tvdb-key", False)

        reconcile_mock.assert_awaited_once_with(fake_db, local_show)
        self.assertEqual(local_show.tvdb_id, 389597)

    async def test_reconcile_skipped_for_a_tmdb_group_order(self) -> None:
        local_show = SimpleNamespace(id=9, tmdb_id=127532, tvdb_id=None)
        fake_db = SimpleNamespace(
            execute=AsyncMock(return_value=_ScalarOneResult(local_show)),
            commit=AsyncMock(),
            add=MagicMock(),
        )

        class _FakeSessionCtx:
            async def __aenter__(self):
                return fake_db

            async def __aexit__(self, *exc):
                return False

        reconcile_mock = AsyncMock()
        with (
            patch("routers.shows.async_sessionmaker", MagicMock(return_value=lambda: _FakeSessionCtx())),
            patch("routers.shows.build_order_positions", AsyncMock(return_value={"positions": 5})),
            patch("routers.shows.get_episode_order", AsyncMock(return_value=None)),
            patch("routers.shows.reconcile_divergent_episode_media", reconcile_mock),
        ):
            await _run_episode_order_mapping(1, 55, 127532, "tmdb:group:abc", "DVD Order", "tmdb-key", None, False)

        reconcile_mock.assert_not_awaited()


class BatchedLookupTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #186: enrich_with_state needs batched forms of
    get_episode_order/the TMDB->TVDB mapping lookup to attach the user's
    episode-order preference and translated positions to every item on a
    page in a fixed number of queries, not one per show."""

    async def test_get_episode_orders_for_series_keys_by_series_tmdb_id(self) -> None:
        rows = [
            UserShowEpisodeOrder(user_id=7, series_tmdb_id=100, episode_order="tvdb", tvdb_id=900),
            UserShowEpisodeOrder(user_id=7, series_tmdb_id=200, episode_order="tmdb", tvdb_id=None),
        ]
        db = AsyncMock()
        db.execute.return_value = _ExistingResult(rows)

        result = await get_episode_orders_for_series(db, user_id=7, series_tmdb_ids=[100, 200, 300])

        self.assertEqual(set(result.keys()), {100, 200})
        self.assertEqual(result[100].episode_order, "tvdb")
        self.assertEqual(result[200].episode_order, "tmdb")

    async def test_get_episode_orders_for_series_empty_input_skips_query(self) -> None:
        db = AsyncMock()
        result = await get_episode_orders_for_series(db, user_id=7, series_tmdb_ids=[])
        self.assertEqual(result, {})
        db.execute.assert_not_called()

    async def test_get_tmdb_to_tvdb_positions_keys_by_series_season_episode(self) -> None:
        rows = [
            EpisodeOrderMapping(
                series_tmdb_id=100, tmdb_season_number=1, tmdb_episode_number=1,
                tmdb_episode_id=1, tvdb_id=901, tvdb_season_number=1, tvdb_episode_number=1,
                match_method="external_id",
            ),
            EpisodeOrderMapping(
                series_tmdb_id=100, tmdb_season_number=4, tmdb_episode_number=12,
                tmdb_episode_id=2, tvdb_id=902, tvdb_season_number=3, tvdb_episode_number=8,
                match_method="title_date",
            ),
        ]
        db = AsyncMock()
        db.execute.return_value = _ExistingResult(rows)

        result = await get_tmdb_to_tvdb_positions(db, series_tmdb_ids=[100])

        self.assertEqual(set(result.keys()), {(100, 1, 1), (100, 4, 12)})
        mapping = result[(100, 4, 12)]
        self.assertEqual((mapping.tvdb_season_number, mapping.tvdb_episode_number), (3, 8))

    async def test_get_tmdb_to_tvdb_positions_empty_input_skips_query(self) -> None:
        db = AsyncMock()
        result = await get_tmdb_to_tvdb_positions(db, series_tmdb_ids=[])
        self.assertEqual(result, {})
        db.execute.assert_not_called()


import os

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy import select as _sa_select
from sqlalchemy.dialects.postgresql import JSONB as _JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker as _async_sessionmaker, create_async_engine as _create_async_engine
from sqlalchemy.ext.compiler import compiles as _compiles
from sqlalchemy.pool import StaticPool as _StaticPool

from core import episode_order as _eo
from core import tmdb as _tmdb, tvdb as _tvdb
from models.episode_order import ShowEpisodePosition


@_compiles(_JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


class OrderKeyHelpersTests(unittest.TestCase):
    def test_normalize_order_key(self) -> None:
        self.assertEqual(_eo.normalize_order_key(None), "tmdb:aired")
        self.assertEqual(_eo.normalize_order_key(""), "tmdb:aired")
        self.assertEqual(_eo.normalize_order_key("tmdb"), "tmdb:aired")
        self.assertEqual(_eo.normalize_order_key("tvdb"), "tvdb:official")
        self.assertEqual(_eo.normalize_order_key("tvdb:dvd"), "tvdb:dvd")
        self.assertEqual(_eo.normalize_order_key("tmdb:group:9"), "tmdb:group:9")

    def test_is_aired_order(self) -> None:
        self.assertTrue(_eo.is_aired_order(None))
        self.assertTrue(_eo.is_aired_order("tmdb"))
        self.assertFalse(_eo.is_aired_order("tvdb"))
        self.assertFalse(_eo.is_aired_order("tvdb:dvd"))

    def test_tvdb_season_type(self) -> None:
        self.assertEqual(_eo._tvdb_season_type("tvdb:dvd"), "dvd")
        self.assertEqual(_eo._tvdb_season_type("tvdb:official"), "official")
        self.assertEqual(_eo._tvdb_season_type("tvdb:type:4271"), "4271")


class OrderPositionsFromTmdbGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_maps_group_and_episode_order_to_display_numbers(self) -> None:
        payload = {
            "groups": [
                {"order": 1, "episodes": [
                    {"id": 20, "season_number": 1, "episode_number": 2, "order": 1},
                    {"id": 10, "season_number": 1, "episode_number": 1, "order": 0},
                ]},
                {"order": 0, "episodes": [
                    {"id": 30, "season_number": 2, "episode_number": 1, "order": 0},
                ]},
            ],
        }
        with patch.object(_tmdb, "get_episode_group", AsyncMock(return_value=payload)):
            rows = await _eo._order_positions_from_tmdb_group(
                99, "tmdb:group:abc", "k", None
            )

        by_eid = {r.tmdb_episode_id: r for r in rows}
        # groups sorted by .order -> the order:0 group is display season 1
        self.assertEqual((by_eid[30].display_season, by_eid[30].display_episode), (1, 1))
        self.assertEqual((by_eid[10].display_season, by_eid[10].display_episode), (2, 1))
        self.assertEqual((by_eid[20].display_season, by_eid[20].display_episode), (2, 2))
        self.assertEqual((by_eid[20].tmdb_season_number, by_eid[20].tmdb_episode_number), (1, 2))


class _PositionsDB(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import models  # noqa: F401
        from models.base import Base

        self.engine = _create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=_StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = _async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)


class BuildOrderPositionsTests(_PositionsDB):
    async def test_tmdb_group_build_persists_positions_and_translates(self) -> None:
        payload = {
            "groups": [
                {"order": 0, "episodes": [
                    {"id": 10, "season_number": 1, "episode_number": 1, "order": 0},
                    {"id": 11, "season_number": 1, "episode_number": 3, "order": 1},
                ]},
                {"order": 1, "episodes": [
                    {"id": 12, "season_number": 1, "episode_number": 2, "order": 0},
                ]},
            ],
        }
        with patch.object(_tmdb, "get_episode_group", AsyncMock(return_value=payload)):
            async with self.Session() as db:
                summary = await _eo.build_order_positions(
                    db, 555, "tmdb:group:xyz", tmdb_api_key="k",
                )
                await db.commit()

        self.assertEqual(summary["positions"], 3)

        async with self.Session() as db:
            # display (1,2) is canonical S1E3
            self.assertEqual(
                await _eo.resolve_display_to_canonical(db, 555, "tmdb:group:xyz", 1, 2),
                (1, 3),
            )
            # aired order is identity
            self.assertEqual(
                await _eo.resolve_display_to_canonical(db, 555, "tmdb:aired", 4, 4),
                (4, 4),
            )
            # unknown position -> None
            self.assertIsNone(
                await _eo.resolve_display_to_canonical(db, 555, "tmdb:group:xyz", 9, 9)
            )

            by_canonical, by_display = await _eo.get_position_maps(db, 555, "tmdb:group:xyz")
            self.assertEqual(by_canonical[(1, 3)].display_episode, 2)
            self.assertEqual(by_display[(2, 1)].tmdb_episode_number, 2)

            batched = await _eo.get_positions_for_series(db, [(555, "tmdb:group:xyz")])
            self.assertEqual(batched[(555, "tmdb:group:xyz")][(1, 1)].display_season, 1)

    async def test_rebuild_is_skipped_without_force(self) -> None:
        payload = {"groups": [{"order": 0, "episodes": [
            {"id": 1, "season_number": 1, "episode_number": 1, "order": 0},
        ]}]}
        mock = AsyncMock(return_value=payload)
        with patch.object(_tmdb, "get_episode_group", mock):
            async with self.Session() as db:
                await _eo.build_order_positions(db, 7, "tmdb:group:a", tmdb_api_key="k")
                await db.commit()
            async with self.Session() as db:
                out = await _eo.build_order_positions(db, 7, "tmdb:group:a", tmdb_api_key="k")
        self.assertTrue(out.get("cached"))
        self.assertEqual(mock.await_count, 1)

    async def test_aired_order_is_a_noop(self) -> None:
        async with self.Session() as db:
            out = await _eo.build_order_positions(db, 1, "tmdb:aired")
        self.assertEqual(out["positions"], 0)

    async def test_tvdb_order_does_not_claim_a_tvdb_id_another_show_holds(self) -> None:
        # Regression: "Locked Up: The Oasis" (TMDB) and its TVDB-only twin
        # "Vis a Vis: El Oasis" are two Show rows for one series. Picking a
        # TVDB ordering on the TMDB row used to assign the twin's tvdb_id to
        # it and die on uq_shows_tvdb_id at autoflush. The id must be used
        # for the fetch without being persisted on the row.
        async with self.Session() as db:
            twin = ShowModel(tvdb_id=364495, tmdb_id=None, title="Vis a Vis: El Oasis", canonical_source="tvdb")
            tmdb_show = ShowModel(tmdb_id=45871, tvdb_id=None, title="Locked Up: The Oasis")
            db.add_all([twin, tmdb_show])
            await db.commit()
            tmdb_show_id = tmdb_show.id

        seen: dict = {}

        async def _fake_type_builder(db, series_tmdb_id, order_key, show_tvdb_id, *args, **kwargs):
            seen["tvdb_id"] = show_tvdb_id
            return [_eo.ShowEpisodePosition(
                series_tmdb_id=series_tmdb_id, order_key=order_key,
                display_season=1, display_episode=1, tmdb_episode_id=1,
                tmdb_season_number=1, tmdb_episode_number=1,
            )]

        with patch.object(_tmdb, "get_show", AsyncMock(return_value={"name": "Locked Up: The Oasis", "external_ids": {"tvdb_id": 364495}})), \
             patch.object(_eo, "_order_positions_from_tvdb_type", _fake_type_builder):
            async with self.Session() as db:
                show = await db.get(ShowModel, tmdb_show_id)
                out = await _eo.build_order_positions(
                    db, 45871, "tvdb:dvd", show=show, tmdb_api_key="k", tvdb_api_key="t",
                )
                await db.commit()

        self.assertEqual(out["positions"], 1)
        self.assertEqual(seen["tvdb_id"], 364495)
        async with self.Session() as db:
            self.assertIsNone((await db.get(ShowModel, tmdb_show_id)).tvdb_id)


class GetOrderKeysForSeriesTests(_PositionsDB):
    async def test_only_non_aired_shows_returned(self) -> None:
        async with self.Session() as db:
            db.add_all([
                UserShowEpisodeOrder(user_id=1, series_tmdb_id=100, episode_order="tvdb"),
                UserShowEpisodeOrder(user_id=1, series_tmdb_id=200, episode_order="tmdb:aired"),
                UserShowEpisodeOrder(user_id=1, series_tmdb_id=300, episode_order="tvdb:dvd"),
            ])
            await db.commit()
        async with self.Session() as db:
            keys = await _eo.get_order_keys_for_series(db, 1, [100, 200, 300, 400])
        self.assertEqual(keys, {100: "tvdb:official", 300: "tvdb:dvd"})


class ListAvailableOrdersTests(unittest.IsolatedAsyncioTestCase):
    async def test_combines_tmdb_groups_and_tvdb_types_with_aired_first(self) -> None:
        groups = {"results": [
            {"id": "g1", "name": "DVD Order", "type": 3, "episode_count": 40},
            {"id": "g2", "name": "Aired", "type": 1, "episode_count": 40},   # skipped (type 1)
            {"id": "g3", "name": "Empty", "type": 5, "episode_count": 0},    # skipped (empty)
        ]}
        series = {"seasons": [
            {"type": {"id": 1, "name": "Aired Order", "type": "official"}},
            {"type": {"id": 2, "name": "DVD Order", "type": "dvd"}},
            {"type": {"id": 4271, "name": "Netflix Order", "type": "netflixorder"}},
        ]}
        show = SimpleNamespace(tvdb_id=121361)
        with patch.object(_tmdb, "get_episode_groups", AsyncMock(return_value=groups)), \
             patch.object(_tvdb, "get_series", AsyncMock(return_value=series)):
            orders = await _eo.list_available_orders(AsyncMock(), 1399, show, "tk", "vk")

        keys = [o["key"] for o in orders]
        self.assertEqual(keys[0], "tmdb:aired")
        self.assertIn("tmdb:group:g1", keys)
        self.assertIn("tvdb:official", keys)
        self.assertIn("tvdb:dvd", keys)
        self.assertIn("tvdb:type:4271", keys)
        self.assertNotIn("tmdb:group:g2", keys)
        self.assertNotIn("tmdb:group:g3", keys)


class LoadTvdbEpisodeIdPositionsTests(_PositionsDB):
    """#447: a Jellyfin/Emby item is resolved through its own TVDB episode id,
    so no assumption about the numbering the server reports is needed."""

    async def test_returns_canonical_position_only_where_orders_disagree(self) -> None:
        from models.episode_order import EpisodeOrderMapping

        def row(tmdb_s, tmdb_e, tvdb_s, tvdb_e, tvdb_id, series=100):
            return EpisodeOrderMapping(
                series_tmdb_id=series, tmdb_season_number=tmdb_s, tmdb_episode_number=tmdb_e,
                tmdb_episode_id=tvdb_id + 1, tvdb_id=tvdb_id,
                tvdb_season_number=tvdb_s, tvdb_episode_number=tvdb_e,
            )

        async with self.Session() as db:
            db.add_all([
                row(2, 79, 2, 21, 4562021),           # diverges
                row(1, 1, 1, 1, 4562001),             # same position both sides
                row(3, 5, 3, 6, 4562022, series=200),  # other series
            ])
            await db.commit()
            got = await _eo.load_tvdb_episode_id_positions(db, [100], [4562021, 4562001, 4562022, 999])

        self.assertEqual(got, {(100, 4562021): (2, 79)})

    async def test_empty_inputs_short_circuit(self) -> None:
        async with self.Session() as db:
            self.assertEqual(await _eo.load_tvdb_episode_id_positions(db, [], [1]), {})
            self.assertEqual(await _eo.load_tvdb_episode_id_positions(db, [1], []), {})


class _DefaultOrderResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class GetDefaultEpisodeOrderTests(unittest.IsolatedAsyncioTestCase):
    """Resolution order: the user's own setting, else the admin's server-wide
    one, else aired order - the same override chain as the TVDB API key. The
    result is a normalised order key, and it only applies to shows with no
    UserShowEpisodeOrder row of their own."""

    def _db(self, own, server):
        """First query reads user_settings, second global_settings."""
        return SimpleNamespace(execute=AsyncMock(side_effect=[
            _DefaultOrderResult(own), _DefaultOrderResult(server),
        ]))

    async def test_nothing_configured_anywhere_means_aired_order(self) -> None:
        self.assertEqual(await get_default_episode_order(self._db(None, None), 1), ORDER_AIRED)

    async def test_the_users_own_choice_wins(self) -> None:
        db = self._db("tmdb:aired", "tvdb:official")
        self.assertEqual(await get_default_episode_order(db, 1), ORDER_AIRED)
        # The server-wide value isn't even read once the user has their own.
        self.assertEqual(db.execute.await_count, 1)

    async def test_the_server_default_is_inherited_when_the_user_has_none(self) -> None:
        self.assertEqual(
            await get_default_episode_order(self._db(None, "tvdb:official"), 1), "tvdb:official"
        )

    async def test_a_legacy_value_is_normalised_on_the_way_out(self) -> None:
        # Rows written before #174 still say "tvdb"; every caller compares
        # order keys, so the resolver has to hand one back.
        self.assertEqual(await get_default_episode_order(self._db("tvdb", None), 1), "tvdb:official")


class _BulkJobDB:
    def __init__(self):
        self.executed = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.add = MagicMock()

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        return _EmptyResult()

    def job_values(self):
        """The bound values of every SyncJob UPDATE this run issued."""
        return [stmt.compile().params for stmt in self.executed]


class RunDefaultEpisodeOrderTests(unittest.IsolatedAsyncioTestCase):
    """The bulk apply behind the settings action: one order for every show the
    user has, overriding per-show choices."""

    def _patches(self, db, apply_order):
        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return (
            patch("routers.shows.async_sessionmaker", MagicMock(return_value=lambda: _Ctx())),
            patch("routers.shows._apply_order_to_show", apply_order),
        )

    async def test_every_show_is_switched_and_the_job_reports_the_count(self) -> None:
        db = _BulkJobDB()
        apply_order = AsyncMock(return_value=({"matched": 1}, 389597))
        p1, p2 = self._patches(db, apply_order)
        with p1, p2:
            await _run_default_episode_order(7, 99, [10, 20, 30], "tvdb:official", "tmdb-key", "tvdb-key")

        self.assertEqual([call.args[2] for call in apply_order.await_args_list], [10, 20, 30])
        # The order key reaches the per-show helper unchanged - an earlier
        # version compared it against "tvdb" and silently applied aired order.
        self.assertEqual({call.args[3] for call in apply_order.await_args_list}, {"tvdb:official"})
        final = db.job_values()[-1]
        self.assertEqual(final["status"], SyncStatus.completed)
        self.assertEqual(final["stats"], {"episode_order": "tvdb:official", "switched": 3, "failed": 0})

    async def test_one_unmappable_show_does_not_abort_the_rest(self) -> None:
        db = _BulkJobDB()
        apply_order = AsyncMock(side_effect=[
            ({"matched": 1}, 1),
            ValueError("No TMDB episodes could be matched to TVDB"),
            ({"matched": 1}, 3),
        ])
        p1, p2 = self._patches(db, apply_order)
        with p1, p2:
            await _run_default_episode_order(7, 99, [10, 20, 30], "tvdb:official", "tmdb-key", "tvdb-key")

        final = db.job_values()[-1]
        self.assertEqual(final["status"], SyncStatus.completed)
        self.assertEqual(final["stats"], {"episode_order": "tvdb:official", "switched": 2, "failed": 1})
        # The failure is reported per show rather than swallowed silently.
        self.assertEqual(final["warnings"][0]["series_tmdb_id"], 20)
        db.rollback.assert_awaited_once()

    async def test_aired_order_goes_through_the_same_helper(self) -> None:
        db = _BulkJobDB()
        apply_order = AsyncMock(return_value=(None, None))
        p1, p2 = self._patches(db, apply_order)
        with p1, p2:
            await _run_default_episode_order(7, 99, [10, 20], ORDER_AIRED, None, None)

        self.assertEqual([call.args[3] for call in apply_order.await_args_list], [ORDER_AIRED, ORDER_AIRED])


class ApplyDefaultToNewShowsTests(unittest.IsolatedAsyncioTestCase):
    """The post-sync hook: catches up shows the user has made no choice for,
    and leaves everything else alone."""

    def _session(self, db):
        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return patch("routers.shows.async_sessionmaker", MagicMock(return_value=lambda: _Ctx()))

    async def test_aired_default_touches_nothing(self) -> None:
        db = _BulkJobDB()
        apply_order = AsyncMock()
        with (
            self._session(db),
            patch("routers.shows.get_default_episode_order", AsyncMock(return_value=ORDER_AIRED)),
            patch("routers.shows.user_series_tmdb_ids", AsyncMock(return_value=[10])) as ids,
            patch("routers.shows._apply_order_to_show", apply_order),
        ):
            await apply_default_episode_order_to_new_shows(7)

        # A show with no preference row already behaves as aired order - the
        # hook must not write a row (or fetch anything) just to say so.
        ids.assert_not_awaited()
        apply_order.assert_not_awaited()

    async def test_only_shows_without_a_choice_are_switched(self) -> None:
        db = _BulkJobDB()
        apply_order = AsyncMock(return_value=({"matched": 1}, 1))
        with (
            self._session(db),
            patch("routers.shows.get_default_episode_order", AsyncMock(return_value="tvdb:official")),
            patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="tmdb-key")),
            patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value="tvdb-key")),
            patch("routers.shows.check_tmdb_key", MagicMock(return_value=True)),
            patch("routers.shows.user_series_tmdb_ids", AsyncMock(return_value=[10, 20, 30])),
            patch("routers.shows.series_tmdb_ids_with_preference", AsyncMock(return_value={20})),
            patch("routers.shows._apply_order_to_show", apply_order),
        ):
            await apply_default_episode_order_to_new_shows(7)

        self.assertEqual([call.args[2] for call in apply_order.await_args_list], [10, 30])

    async def test_missing_tvdb_key_is_not_an_error(self) -> None:
        db = _BulkJobDB()
        apply_order = AsyncMock()
        with (
            self._session(db),
            patch("routers.shows.get_default_episode_order", AsyncMock(return_value="tvdb:official")),
            patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="tmdb-key")),
            patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value=None)),
            patch("routers.shows.check_tmdb_key", MagicMock(return_value=True)),
            patch("routers.shows._apply_order_to_show", apply_order),
        ):
            await apply_default_episode_order_to_new_shows(7)

        apply_order.assert_not_awaited()


class ApplyDefaultEpisodeOrderEndpointTests(unittest.IsolatedAsyncioTestCase):
    """POST /shows/episode-order/apply-default - the settings action."""

    async def test_tvdb_without_a_key_is_rejected_before_any_job_is_queued(self) -> None:
        db = _BulkJobDB()
        tasks = SimpleNamespace(add_task=MagicMock())
        with (
            patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="tmdb-key")),
            patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value=None)),
            patch("routers.shows.check_tmdb_key", MagicMock(return_value=True)),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await apply_default_episode_order(
                    ApplyDefaultEpisodeOrderRequest(order="tvdb"),
                    tasks, db=db, current_user=SimpleNamespace(id=7),
                )
        self.assertEqual(ctx.exception.status_code, 400)
        tasks.add_task.assert_not_called()

    async def test_an_unknown_order_is_rejected(self) -> None:
        db = _BulkJobDB()
        tasks = SimpleNamespace(add_task=MagicMock())
        with self.assertRaises(HTTPException) as ctx:
            await apply_default_episode_order(
                ApplyDefaultEpisodeOrderRequest(order="imdb"),
                tasks, db=db, current_user=SimpleNamespace(id=7),
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_a_user_with_no_shows_starts_no_job(self) -> None:
        db = _BulkJobDB()
        tasks = SimpleNamespace(add_task=MagicMock())
        with patch("routers.shows.user_series_tmdb_ids", AsyncMock(return_value=[])):
            result = await apply_default_episode_order(
                ApplyDefaultEpisodeOrderRequest(order="tmdb"),
                tasks, db=db, current_user=SimpleNamespace(id=7),
            )
        self.assertEqual(result, {"status": "noop", "shows": 0, "episode_order": ORDER_AIRED})
        tasks.add_task.assert_not_called()

    async def test_the_queued_job_gets_the_normalised_order_key(self) -> None:
        db = _BulkJobDB()
        db.refresh = AsyncMock()
        tasks = SimpleNamespace(add_task=MagicMock())
        with (
            patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="tmdb-key")),
            patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value="tvdb-key")),
            patch("routers.shows.check_tmdb_key", MagicMock(return_value=True)),
            patch("routers.shows.user_series_tmdb_ids", AsyncMock(return_value=[10, 20])),
        ):
            result = await apply_default_episode_order(
                ApplyDefaultEpisodeOrderRequest(order="tvdb"),
                tasks, db=db, current_user=SimpleNamespace(id=7),
            )
        self.assertEqual(result["episode_order"], "tvdb:official")
        self.assertEqual(result["shows"], 2)
        self.assertEqual(tasks.add_task.call_args.args[3], [10, 20])
        self.assertEqual(tasks.add_task.call_args.args[4], "tvdb:official")

    async def test_the_saved_default_is_used_when_no_order_is_passed(self) -> None:
        db = _BulkJobDB()
        db.refresh = AsyncMock()
        tasks = SimpleNamespace(add_task=MagicMock())
        with (
            patch("routers.shows.get_default_episode_order", AsyncMock(return_value=ORDER_AIRED)),
            patch("routers.shows.user_series_tmdb_ids", AsyncMock(return_value=[10, 20])),
        ):
            result = await apply_default_episode_order(
                ApplyDefaultEpisodeOrderRequest(),
                tasks, db=db, current_user=SimpleNamespace(id=7),
            )
        self.assertEqual(result["episode_order"], ORDER_AIRED)
        self.assertEqual(tasks.add_task.call_args.args[4], ORDER_AIRED)


if __name__ == "__main__":
    unittest.main()

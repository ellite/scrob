import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from core.episode_order import ensure_episode_order_mapping, validate_episode_order
from models.base import MediaType
from models.media import Media
from models.ratings import Rating
from models.show import Show as ShowModel
from routers.ratings import RatingIn, submit_rating
from routers.shows import _enrich_tvdb_seasons, get_tvdb_season


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
                    _ExistingResult([(201,), (202,)]),                  # watched_q — both watched
                    _ExistingResult([(201,)]),                          # collected_q — only 201 collected
                    _ExistingResult([]),                                # episode_ratings_q
                    _ScalarOneResult(None),                             # show_media_result
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
        self.assertEqual(validate_episode_order("tmdb"), "tmdb")
        self.assertEqual(validate_episode_order("tvdb"), "tvdb")
        with self.assertRaisesRegex(ValueError, "Unsupported episode order"):
            validate_episode_order("absolute")


if __name__ == "__main__":
    unittest.main()

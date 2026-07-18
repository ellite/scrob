import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.base import MediaType
from models.media import Media
from routers.mdblist import _import_ratings
from routers.trakt import _apply_imported_rating
from routers.sync import _fan_out_changes_to_other_connections


def _scalar_result(items: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


def _rows_result(items: list[tuple]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = items
    return result


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        tmdb_api_key="tmdb-key",
        trakt_push_watched=False,
        trakt_push_ratings=True,
        trakt_access_token="trakt-token",
        trakt_client_id="trakt-client",
        mdblist_push_watched=False,
        mdblist_push_ratings=True,
        mdblist_api_key="mdblist-key",
        simkl_push_ratings=False,
        simkl_access_token=None,
        simkl_client_id=None,
    )


def _plex_connection() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        type="plex",
        url="http://plex.local",
        token="plex-token",
        server_user_id=None,
        push_watched=False,
        push_ratings=True,
    )


class SeasonRatingFanoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_season_rating_fans_out_with_provider_specific_identity(self) -> None:
        media = Media(
            id=1,
            tmdb_id=1396,
            media_type=MediaType.series,
            title="Breaking Bad",
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    _scalar_result([media]),
                    _scalar_result([_plex_connection()]),
                    _rows_result([]),
                    _rows_result([(1, 1, datetime(2026, 7, 18, 0, 0, 0))]),
                ]
            ),
            commit=AsyncMock(),
        )

        with (
            patch("routers.sync._resolve_tmdb_season_ids", AsyncMock(return_value={(1, 1): 3572})),
            patch("routers.sync.plex.resolve_season_rating_key", AsyncMock(return_value="103")) as resolve_plex,
            patch("routers.sync.plex.set_rating", AsyncMock(return_value=True)) as set_plex,
            patch("routers.sync.trakt_client.set_ratings_batch", AsyncMock()) as set_trakt,
            patch("core.mdblist.push_ratings", AsyncMock(return_value={})) as push_mdblist,
        ):
            await _fan_out_changes_to_other_connections(
                db,
                user_id=3,
                exclude_connection_id=None,
                new_watched_ids=set(),
                new_ratings={(1, 1): 8.0},
                settings=_settings(),
            )

        resolve_plex.assert_awaited_once_with(
            "http://plex.local",
            "plex-token",
            1396,
            1,
        )
        set_plex.assert_awaited_once_with(
            "http://plex.local",
            "plex-token",
            "103",
            8.0,
        )
        set_trakt.assert_awaited_once_with(
            "trakt-client",
            "trakt-token",
            [],
            [],
            [(3572, 8.0)],
        )
        payload = push_mdblist.await_args.args[1]
        self.assertEqual(
            payload["shows"],
            [
                {
                    "ids": {"tmdb": 1396},
                    "seasons": [
                        {
                            "number": 1,
                            "rating": 8.0,
                            "rated_at": "2026-07-18T00:00:00Z",
                        }
                    ],
                }
            ],
        )

    async def test_season_rating_removal_fans_out_without_touching_show_rating(self) -> None:
        media = Media(
            id=1,
            tmdb_id=1396,
            media_type=MediaType.series,
            title="Breaking Bad",
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    _scalar_result([media]),
                    _scalar_result([_plex_connection()]),
                    _rows_result([]),
                ]
            ),
            commit=AsyncMock(),
        )

        with (
            patch("routers.sync._resolve_tmdb_season_ids", AsyncMock(return_value={(1, 1): 3572})),
            patch("routers.sync.plex.resolve_season_rating_key", AsyncMock(return_value="103")),
            patch("routers.sync.plex.set_rating", AsyncMock(return_value=True)) as set_plex,
            patch("routers.sync.trakt_client.remove_ratings_batch", AsyncMock()) as remove_trakt,
            patch("core.mdblist.remove_ratings", AsyncMock(return_value={})) as remove_mdblist,
        ):
            await _fan_out_changes_to_other_connections(
                db,
                user_id=3,
                exclude_connection_id=None,
                new_watched_ids=set(),
                new_ratings={},
                settings=_settings(),
                removed_ratings={(1, 1)},
            )

        set_plex.assert_awaited_once_with(
            "http://plex.local",
            "plex-token",
            "103",
            0.0,
        )
        remove_trakt.assert_awaited_once_with(
            "trakt-client",
            "trakt-token",
            [],
            [],
            [3572],
        )
        payload = remove_mdblist.await_args.args[1]
        self.assertEqual(
            payload["shows"],
            [{"ids": {"tmdb": 1396}, "seasons": [{"number": 1}]}],
        )


class SeasonRatingImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_mdblist_import_persists_season_rating_separately(self) -> None:
        media = Media(
            id=1,
            tmdb_id=1396,
            media_type=MediaType.series,
            title="Breaking Bad",
        )
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=False)
        db = SimpleNamespace(
            execute=AsyncMock(return_value=_scalar_result([])),
            begin_nested=MagicMock(return_value=transaction),
            add=MagicMock(),
        )
        stats = {"ratings": 0, "skipped": 0, "errors": 0}
        payload = {
            "seasons": [
                {
                    "rating": 8,
                    "rated_at": "2026-07-18T00:00:00Z",
                    "season": {
                        "number": 1,
                        "ids": {"tmdb": 3572},
                        "show": {
                            "title": "Breaking Bad",
                            "ids": {"tmdb": 1396},
                        },
                    },
                }
            ]
        }

        with (
            patch(
                "routers.mdblist._resolve_external_tmdb_id",
                AsyncMock(return_value=1396),
            ),
            patch(
                "routers.mdblist._get_or_create_series_media",
                AsyncMock(return_value=media),
            ),
        ):
            changed = await _import_ratings(
                db,
                user_id=3,
                payload=payload,
                api_key="tmdb-key",
                external_cache={},
                stats=stats,
            )

        self.assertEqual(changed, {(1, 1): 8.0})
        self.assertEqual(stats["ratings"], 1)
        imported = db.add.call_args.args[0]
        self.assertEqual(imported.media_id, 1)
        self.assertEqual(imported.season_number, 1)
        self.assertEqual(imported.rating, 8.0)

    def test_trakt_import_keeps_show_and_season_ratings_distinct(self) -> None:
        media = Media(
            id=1,
            tmdb_id=1396,
            media_type=MediaType.series,
            title="Breaking Bad",
        )
        db = SimpleNamespace(add=MagicMock())
        existing = {}
        changed = {}

        self.assertTrue(
            _apply_imported_rating(
                db,
                user_id=3,
                media=media,
                season_number=None,
                item={"rating": 9, "rated_at": "2026-07-18T00:00:00Z"},
                existing=existing,
                changed=changed,
            )
        )
        self.assertTrue(
            _apply_imported_rating(
                db,
                user_id=3,
                media=media,
                season_number=1,
                item={"rating": 8, "rated_at": "2026-07-18T00:00:00Z"},
                existing=existing,
                changed=changed,
            )
        )

        self.assertEqual(changed, {(1, None): 9.0, (1, 1): 8.0})
        self.assertEqual(existing[(1, None)].season_number, None)
        self.assertEqual(existing[(1, 1)].season_number, 1)


if __name__ == "__main__":
    unittest.main()

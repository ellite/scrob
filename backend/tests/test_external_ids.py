import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import external_ids


def _row(source, external_id, media_kind, tmdb_id, miss_count=0, age=timedelta(0)):
    """Stands in for an ExternalIdMapping row loaded from the table."""
    return SimpleNamespace(
        source=source,
        external_id=external_id,
        media_kind=media_kind,
        tmdb_id=tmdb_id,
        miss_count=miss_count,
        checked_at=datetime.utcnow() - age,
    )


class MakePairTests(unittest.TestCase):
    def test_normalises_and_validates(self) -> None:
        self.assertEqual(
            external_ids.make_pair(external_ids.IMDB, "TT0903747", external_ids.TV),
            ("imdb_id", "tt0903747", "tv"),
        )
        self.assertEqual(
            external_ids.make_pair(external_ids.TVDB, 81189, external_ids.TV),
            ("tvdb_id", "81189", "tv"),
        )

    def test_rejects_unusable_input(self) -> None:
        for source, value, kind in (
            (external_ids.IMDB, "not-an-imdb-id", external_ids.TV),
            (external_ids.IMDB, "", external_ids.TV),
            (external_ids.IMDB, None, external_ids.TV),
            (external_ids.TVDB, "abc", external_ids.TV),
            (external_ids.IMDB, "tt1", "nonsense_kind"),
            ("bogus_source", "tt1", external_ids.TV),
        ):
            with self.subTest(source=source, value=value, kind=kind):
                self.assertIsNone(external_ids.make_pair(source, value, kind))


class ResolveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        external_ids.reset_memo()

    async def test_cached_positive_never_calls_tmdb(self) -> None:
        pair = ("imdb_id", "tt0903747", "tv")
        find = AsyncMock()
        with patch.object(external_ids, "_load", AsyncMock(return_value={pair: _row(*pair, 1396)})), \
             patch.object(external_ids, "_store", AsyncMock()) as store, \
             patch.object(external_ids.tmdb, "find_by_external_id", find):
            got = await external_ids.resolve_many([pair], "key")

        self.assertEqual(got, {pair: 1396})
        find.assert_not_awaited()
        store.assert_not_awaited()

    async def test_one_find_call_serves_every_kind_of_the_same_id(self) -> None:
        movie = ("imdb_id", "tt0903747", "movie")
        tv = ("imdb_id", "tt0903747", "tv")
        find = AsyncMock(return_value={"tv_results": [{"id": 1396}], "movie_results": []})

        with patch.object(external_ids, "_load", AsyncMock(return_value={})), \
             patch.object(external_ids, "_store", AsyncMock()) as store, \
             patch.object(external_ids.tmdb, "find_by_external_id", find):
            got = await external_ids.resolve_many([movie, tv], "key")

        self.assertEqual(find.await_count, 1)
        self.assertEqual(got, {movie: None, tv: 1396})
        # The hit is stored, and so is the negative for the kind that was asked about.
        stored = {(r["media_kind"], r["tmdb_id"]) for r in store.await_args.args[0]}
        self.assertIn(("tv", 1396), stored)
        self.assertIn(("movie", None), stored)

    async def test_unasked_buckets_are_still_recorded(self) -> None:
        """The response is already paid for; another caller will want the rest."""
        tv = ("imdb_id", "tt0903747", "tv")
        find = AsyncMock(return_value={
            "tv_results": [{"id": 1396}],
            "tv_episode_results": [{"id": 62085, "show_id": 1396}],
        })
        with patch.object(external_ids, "_load", AsyncMock(return_value={})), \
             patch.object(external_ids, "_store", AsyncMock()) as store, \
             patch.object(external_ids.tmdb, "find_by_external_id", find):
            await external_ids.resolve_many([tv], "key")

        stored = {r["media_kind"]: r["tmdb_id"] for r in store.await_args.args[0]}
        # tv_episode_show stores the PARENT SHOW id, not the episode id.
        self.assertEqual(stored["tv_episode_show"], 1396)
        self.assertNotEqual(stored["tv_episode_show"], 62085)

    async def test_failed_tmdb_call_is_not_cached_as_a_negative(self) -> None:
        """A timeout or 5xx must never be persisted as 'TMDB has no match'."""
        pair = ("imdb_id", "tt0903747", "tv")
        find = AsyncMock(side_effect=RuntimeError("connection reset"))

        with patch.object(external_ids, "_load", AsyncMock(return_value={})), \
             patch.object(external_ids, "_store", AsyncMock()) as store, \
             patch.object(external_ids.tmdb, "find_by_external_id", find):
            got = await external_ids.resolve_many([pair], "key")

        self.assertEqual(got, {pair: None})
        self.assertEqual(store.await_args.args[0], [])

    async def test_fresh_negative_is_served_without_calling_tmdb(self) -> None:
        pair = ("imdb_id", "tt9999999", "movie")
        find = AsyncMock()
        with patch.object(external_ids, "_load",
                          AsyncMock(return_value={pair: _row(*pair, None, miss_count=0, age=timedelta(hours=2))})), \
             patch.object(external_ids, "_store", AsyncMock()), \
             patch.object(external_ids.tmdb, "find_by_external_id", find):
            got = await external_ids.resolve_many([pair], "key")

        self.assertEqual(got, {pair: None})
        find.assert_not_awaited()

    async def test_stale_negative_is_retried(self) -> None:
        pair = ("imdb_id", "tt9999999", "movie")
        find = AsyncMock(return_value={"movie_results": [{"id": 42}]})
        with patch.object(external_ids, "_load",
                          AsyncMock(return_value={pair: _row(*pair, None, miss_count=0, age=timedelta(days=3))})), \
             patch.object(external_ids, "_store", AsyncMock()), \
             patch.object(external_ids.tmdb, "find_by_external_id", find):
            got = await external_ids.resolve_many([pair], "key")

        find.assert_awaited_once()
        self.assertEqual(got, {pair: 42})

    async def test_negative_backoff_widens_with_miss_count(self) -> None:
        pair = ("imdb_id", "tt9999999", "movie")
        # 3 misses -> 8 day window, so a 3-day-old negative is still fresh
        # even though it would have been retried at miss_count=0.
        find = AsyncMock()
        with patch.object(external_ids, "_load",
                          AsyncMock(return_value={pair: _row(*pair, None, miss_count=3, age=timedelta(days=3))})), \
             patch.object(external_ids, "_store", AsyncMock()), \
             patch.object(external_ids.tmdb, "find_by_external_id", find):
            await external_ids.resolve_many([pair], "key")
        find.assert_not_awaited()

    async def test_no_api_key_serves_cache_only_and_never_calls_tmdb(self) -> None:
        pair = ("imdb_id", "tt0903747", "tv")
        find = AsyncMock()
        with patch.object(external_ids, "_load", AsyncMock(return_value={})), \
             patch.object(external_ids, "_store", AsyncMock()), \
             patch.object(external_ids.tmdb, "find_by_external_id", find):
            got = await external_ids.resolve_many([pair], None)

        self.assertEqual(got, {pair: None})
        find.assert_not_awaited()

    async def test_memo_short_circuits_a_second_call(self) -> None:
        pair = ("imdb_id", "tt0903747", "tv")
        load = AsyncMock(return_value={pair: _row(*pair, 1396)})
        with patch.object(external_ids, "_load", load), \
             patch.object(external_ids, "_store", AsyncMock()), \
             patch.object(external_ids.tmdb, "find_by_external_id", AsyncMock()):
            await external_ids.resolve_many([pair], "key")
            await external_ids.resolve_many([pair], "key")

        self.assertEqual(load.await_count, 1)

    async def test_resolve_first_returns_the_first_hit_in_order(self) -> None:
        imdb = ("imdb_id", "tt0903747", "tv")
        tvdb = ("tvdb_id", "81189", "tv")
        with patch.object(external_ids, "_load",
                          AsyncMock(return_value={imdb: _row(*imdb, 1396), tvdb: _row(*tvdb, 9999)})), \
             patch.object(external_ids, "_store", AsyncMock()), \
             patch.object(external_ids.tmdb, "find_by_external_id", AsyncMock()):
            got = await external_ids.resolve_first(
                [(external_ids.IMDB, "tt0903747"), (external_ids.TVDB, 81189)],
                external_ids.TV,
                "key",
            )
        self.assertEqual(got, 1396)

    async def test_resolve_first_falls_through_a_miss(self) -> None:
        tvdb = ("tvdb_id", "81189", "tv")
        with patch.object(external_ids, "_load",
                          AsyncMock(return_value={tvdb: _row(*tvdb, 9999)})), \
             patch.object(external_ids, "_store", AsyncMock()), \
             patch.object(external_ids.tmdb, "find_by_external_id",
                          AsyncMock(return_value={"tv_results": []})):
            got = await external_ids.resolve_first(
                [(external_ids.IMDB, "tt0000000"), (external_ids.TVDB, 81189)],
                external_ids.TV,
                "key",
            )
        self.assertEqual(got, 9999)

    async def test_resolve_one_rejects_a_malformed_id_without_any_io(self) -> None:
        load = AsyncMock()
        with patch.object(external_ids, "_load", load):
            got = await external_ids.resolve_one(external_ids.IMDB, "garbage", external_ids.TV, "key")
        self.assertIsNone(got)
        load.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class NegativeMustNotClobberPositiveTests(unittest.IsolatedAsyncioTestCase):
    """The upsert used to write excluded.tmdb_id unconditionally, so an empty
    /find result overwrote an id that record() had taken from TMDB's own
    external_ids endpoint — and started the negative backoff, hiding the
    correct mapping for up to 30 days."""

    def setUp(self) -> None:
        external_ids.reset_memo()

    async def test_resolve_first_stops_at_the_first_hit(self) -> None:
        """It used to hand every candidate to resolve_many, fetching a TVDB id
        for a caller whose IMDb id had already resolved."""
        asked = []

        async def fake_resolve_many(pairs, api_key):
            pairs = list(pairs)
            asked.extend(pairs)
            return {p: (1396 if p[0] == "imdb_id" else 9999) for p in pairs}

        with patch.object(external_ids, "resolve_many", side_effect=fake_resolve_many):
            got = await external_ids.resolve_first(
                [(external_ids.IMDB, "tt0903747"), (external_ids.TVDB, 81189)],
                external_ids.TV, "key",
            )

        self.assertEqual(got, 1396)
        self.assertEqual(asked, [("imdb_id", "tt0903747", "tv")])  # tvdb never asked


class MalformedPayloadTests(unittest.IsolatedAsyncioTestCase):
    """resolve_many promises never to raise for an individual id — callers rely
    on that to fall through to their own fallbacks."""

    def setUp(self) -> None:
        external_ids.reset_memo()

    async def test_a_non_dict_payload_does_not_escape(self) -> None:
        pair = ("imdb_id", "tt0903747", "tv")
        with patch.object(external_ids, "_load", AsyncMock(return_value={})), \
             patch.object(external_ids, "_store", AsyncMock()), \
             patch.object(external_ids.tmdb, "find_by_external_id",
                          AsyncMock(return_value=["unexpected"])):
            got = await external_ids.resolve_many([pair], "key")
        self.assertEqual(got, {pair: None})

    async def test_a_non_dict_match_entry_does_not_escape(self) -> None:
        pair = ("imdb_id", "tt0903747", "tv")
        with patch.object(external_ids, "_load", AsyncMock(return_value={})), \
             patch.object(external_ids, "_store", AsyncMock()), \
             patch.object(external_ids.tmdb, "find_by_external_id",
                          AsyncMock(return_value={"tv_results": ["not-a-dict"]})):
            got = await external_ids.resolve_many([pair], "key")
        self.assertEqual(got, {pair: None})


class RecordManyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        external_ids.reset_memo()

    async def test_a_batch_is_stored_in_one_call(self) -> None:
        with patch.object(external_ids, "_store", AsyncMock()) as store:
            await external_ids.record_many([
                (external_ids.IMDB, "tt0903747", external_ids.TV, 1396),
                (external_ids.TVDB, 81189, external_ids.TV, 1396),
                (external_ids.IMDB, "garbage", external_ids.TV, 1),   # dropped
                (external_ids.IMDB, "tt0111161", external_ids.MOVIE, 0),  # dropped
            ])
        store.assert_awaited_once()
        rows = store.await_args.args[0]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["external_id"] for r in rows}, {"tt0903747", "81189"})

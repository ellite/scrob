import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from core import plex


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class GetHistorySinceCursorTests(unittest.IsolatedAsyncioTestCase):
    """Regression (#126): plex_history_cursor_at is stored as naive UTC (the
    codebase-wide convention), but datetime.timestamp() on a naive datetime
    interprets it as *local* time - west of UTC that shifted the viewedAt>
    filter hours too late, and since the cursor still advances, those plays
    were then skipped forever. The fix attaches tzinfo=utc before calling
    .timestamp() so the epoch value is correct independent of the host's
    system timezone."""

    async def test_naive_since_is_treated_as_utc_not_local_time(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["viewedAt>"] = request.url.params.get("viewedAt>")
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [], "totalSize": 0}})

        transport = httpx.MockTransport(handler)
        naive_since = datetime(2026, 1, 1, 0, 0, 0)  # naive, UTC semantics
        expected_epoch = int(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())

        with patch.object(
            plex.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await plex.get_history("http://plex.local", "token", since=naive_since)

        self.assertEqual(captured["viewedAt>"], str(expected_epoch))


class PlexSeasonRatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_season_rating_key_uses_parent_show_tmdb_id(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/library/sections/all":
                self.assertEqual(request.url.params["type"], "2")
                self.assertEqual(request.url.params["guid"], "tmdb://1396")
                return httpx.Response(
                    200,
                    json={
                        "MediaContainer": {
                            "Metadata": [
                                {
                                    "ratingKey": "100",
                                    "Guid": [{"id": "tmdb://1396"}],
                                }
                            ]
                        }
                    },
                )
            if request.url.path == "/library/metadata/100/children":
                return httpx.Response(
                    200,
                    json={
                        "MediaContainer": {
                            "Metadata": [
                                {"ratingKey": "101", "index": 0, "type": "season"},
                                {"ratingKey": "102", "index": 1, "type": "season"},
                                {"ratingKey": "103", "index": 2, "type": "season"},
                            ]
                        }
                    },
                )
            self.fail(f"Unexpected Plex path: {request.url.path}")

        transport = httpx.MockTransport(handler)
        with patch.object(
            plex.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            rating_key = await plex.resolve_season_rating_key(
                "http://plex.local",
                "token",
                1396,
                2,
            )

        self.assertEqual(rating_key, "103")
        self.assertEqual(
            requested_paths,
            ["/library/sections/all", "/library/metadata/100/children"],
        )

    async def test_zero_rating_clears_plex_season_rating(self) -> None:
        request_data: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            request_data["path"] = request.url.path
            request_data["key"] = request.url.params["key"]
            request_data["identifier"] = request.url.params["identifier"]
            request_data["rating"] = request.url.params["rating"]
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        async with _REAL_ASYNC_CLIENT(transport=transport) as client:
            result = await plex.set_rating(
                "http://plex.local",
                "token",
                "103",
                0,
                client=client,
            )

        self.assertTrue(result)
        self.assertEqual(
            request_data,
            {
                "path": "/:/rate",
                "key": "103",
                "identifier": "com.plexapp.plugins.library",
                "rating": "0",
            },
        )


def _search_response(rating_keys: list[str], section_id: str = "external") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "MediaContainer": {
                "SearchResults": [
                    {
                        "id": section_id,
                        "SearchResult": [
                            {"Metadata": {"type": "movie", "ratingKey": rk}} for rk in rating_keys
                        ],
                    }
                ]
            }
        },
    )


def _metadata_response(guids: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"MediaContainer": {"Metadata": [{"Guid": [{"id": g} for g in guids]}]}},
    )


class ResolveTmdbRatingkeyTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for issues #119/#83: the original implementation
    queried `/library/sections/computer/all?guid=...` on Discover, which
    isn't a real Discover section and always came back empty, so pushing to
    the Plex watchlist silently no-op'd. The fix searches `/library/search`
    by title (Discover's actual documented pattern, per python-plexapi's
    MyPlexAccount.searchDiscover) - but that search's results never carry
    Guid data even with includeMetadata=1 (confirmed live), so each
    candidate's ratingKey is separately verified via enrich_plex_item's full
    metadata lookup (which does carry Guid) until the exact tmdb match is
    found."""

    async def test_matches_the_right_candidate_by_verifying_each_ratingkey(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/library/search":
                self.assertEqual(request.url.params["query"], "Fight Club")
                self.assertEqual(request.url.params["searchTypes"], "movies")
                return _search_response(["wrong", "right"])
            if request.url.path == "/library/metadata/wrong":
                return _metadata_response(["tmdb://999"])
            if request.url.path == "/library/metadata/right":
                return _metadata_response(["tmdb://550"])
            self.fail(f"Unexpected Plex path: {request.url.path}")

        transport = httpx.MockTransport(handler)
        with patch.object(
            plex.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            rating_key = await plex.resolve_tmdb_ratingkey("token", 550, "movie", "Fight Club")

        self.assertEqual(rating_key, "right")
        # Stops as soon as the match is found - doesn't keep checking further.
        self.assertEqual(requested_paths, ["/library/search", "/library/metadata/wrong", "/library/metadata/right"])

    async def test_no_match_returns_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/library/search":
                return _search_response(["other"])
            if request.url.path == "/library/metadata/other":
                return _metadata_response(["tmdb://1"])
            self.fail(f"Unexpected Plex path: {request.url.path}")

        transport = httpx.MockTransport(handler)
        with patch.object(
            plex.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            rating_key = await plex.resolve_tmdb_ratingkey("token", 550, "movie", "Fight Club")

        self.assertIsNone(rating_key)

    async def test_search_results_without_guid_data_still_resolve_via_enrichment(self) -> None:
        """The exact live-reproduced bug: /library/search returns candidates
        with Guid=None even for an exact title match - resolution must not
        depend on Guid data being present in the search response itself."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/library/search":
                return _search_response(["right"])  # no Guid in this response at all
            if request.url.path == "/library/metadata/right":
                return _metadata_response(["tmdb://550"])
            self.fail(f"Unexpected Plex path: {request.url.path}")

        transport = httpx.MockTransport(handler)
        with patch.object(
            plex.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            rating_key = await plex.resolve_tmdb_ratingkey("token", 550, "movie", "Project Hail Mary")

        self.assertEqual(rating_key, "right")

    async def test_non_external_search_sections_are_ignored(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/library/search":
                return _search_response(["local-only"], section_id="server")
            self.fail(f"Unexpected Plex path: {request.url.path}")

        transport = httpx.MockTransport(handler)
        with patch.object(
            plex.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            rating_key = await plex.resolve_tmdb_ratingkey("token", 550, "movie", "Fight Club")

        self.assertIsNone(rating_key)

    async def test_stops_checking_after_candidate_limit(self) -> None:
        many_keys = [f"key{i}" for i in range(plex._RESOLVE_CANDIDATE_CHECK_LIMIT + 5)]
        checked_metadata_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/library/search":
                return _search_response(many_keys)
            checked_metadata_paths.append(request.url.path)
            return _metadata_response(["tmdb://999999"])  # never matches

        transport = httpx.MockTransport(handler)
        with patch.object(
            plex.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            rating_key = await plex.resolve_tmdb_ratingkey("token", 550, "movie", "Fight Club")

        self.assertIsNone(rating_key)
        self.assertEqual(len(checked_metadata_paths), plex._RESOLVE_CANDIDATE_CHECK_LIMIT)


def _g(*ids: str) -> list[dict]:
    return [{"id": i} for i in ids]


class GuidIdExtractionTests(unittest.TestCase):
    """extract_{tmdb,tvdb,imdb}_id must read the plain scheme, the legacy
    com.plexapp.agents.* form, and the HAMA / Absolute Series Scanner agent
    form (com.plexapp.agents.hama://tvdb-73762/1/1), without mistaking an
    unrelated GUID for an id (PR #337)."""

    # ── TMDB ──────────────────────────────────────────────────────────────
    def test_tmdb_plain_and_legacy_schemes(self):
        self.assertEqual(plex.extract_tmdb_id(_g("tmdb://1396")), 1396)
        self.assertEqual(
            plex.extract_tmdb_id(_g("com.plexapp.agents.themoviedb://1396?lang=en")), 1396
        )

    def test_tmdb_hama_form(self):
        self.assertEqual(
            plex.extract_tmdb_id(_g("com.plexapp.agents.hama://tmdb-1396/1/1")), 1396
        )

    def test_tmdb_returns_int_or_none(self):
        self.assertIsInstance(plex.extract_tmdb_id(_g("tmdb://5")), int)
        self.assertIsNone(plex.extract_tmdb_id(_g("imdb://tt0111161", "plex://movie/abc")))
        self.assertIsNone(plex.extract_tmdb_id([]))
        self.assertIsNone(plex.extract_tmdb_id(_g("com.plexapp.agents.hama://anidb-999/1/1")))

    # ── TVDB ──────────────────────────────────────────────────────────────
    def test_tvdb_plain_and_legacy_schemes(self):
        self.assertEqual(plex.extract_tvdb_id(_g("tvdb://73762")), "73762")
        self.assertEqual(
            plex.extract_tvdb_id(_g("com.plexapp.agents.thetvdb://73762/1/1")), "73762"
        )

    def test_tvdb_hama_form_including_alternate_order_suffix(self):
        self.assertEqual(
            plex.extract_tvdb_id(_g("com.plexapp.agents.hama://tvdb-73762/1/1")), "73762"
        )
        # HAMA uses tvdb2-/tvdb3-... for TheTVDB's alternate episode orders;
        # we still want the series id.
        self.assertEqual(
            plex.extract_tvdb_id(_g("com.plexapp.agents.hama://tvdb2-73762/1/5")), "73762"
        )

    def test_tvdb_ignores_other_sources(self):
        self.assertIsNone(plex.extract_tvdb_id(_g("tmdb://1396", "imdb://tt0903747")))
        self.assertIsNone(plex.extract_tvdb_id(_g("com.plexapp.agents.hama://anidb-999/1/1")))
        self.assertIsNone(plex.extract_tvdb_id([]))

    # ── IMDB ──────────────────────────────────────────────────────────────
    def test_imdb_all_forms(self):
        self.assertEqual(plex.extract_imdb_id(_g("imdb://tt0111161")), "tt0111161")
        self.assertEqual(
            plex.extract_imdb_id(_g("com.plexapp.agents.imdb://tt0111161")), "tt0111161"
        )
        self.assertEqual(
            plex.extract_imdb_id(_g("com.plexapp.agents.hama://imdb-tt0111161/1/1")), "tt0111161"
        )

    def test_imdb_requires_the_tt_prefix(self):
        self.assertIsNone(plex.extract_imdb_id(_g("tmdb://1396")))
        self.assertIsNone(plex.extract_imdb_id(_g("imdb://12345")))
        self.assertIsNone(plex.extract_imdb_id([]))

    # ── ordering / get_guids ─────────────────────────────────────────────
    def test_first_matching_guid_in_the_list_wins(self):
        guids = _g("plex://episode/5d9c", "tvdb://73762", "tvdb://99999")
        self.assertEqual(plex.extract_tvdb_id(guids), "73762")

    def test_get_guids_wraps_a_legacy_hama_string(self):
        item = {"guid": "com.plexapp.agents.hama://tvdb-73762/1/1"}
        guids = plex.get_guids(item)
        self.assertEqual(guids, [{"id": "com.plexapp.agents.hama://tvdb-73762/1/1"}])
        self.assertEqual(plex.extract_tvdb_id(guids), "73762")


if __name__ == "__main__":
    unittest.main()

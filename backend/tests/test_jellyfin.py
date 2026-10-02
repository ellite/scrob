import json
import unittest
from unittest.mock import patch

import httpx

from core import jellyfin


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class JellyfinEpisodeQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_episodes_excludes_virtual_missing_episodes(self) -> None:
        requested_params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/Users/user-id/Items")
            requested_params.update(request.url.params)
            return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            episodes = await jellyfin.get_episodes(
                "library-id", "http://jellyfin.local", "token", "user-id"
            )

        self.assertEqual(episodes, [])
        self.assertEqual(requested_params["IncludeItemTypes"], "Episode")
        self.assertEqual(requested_params["ExcludeLocationTypes"], "Virtual")
        self.assertEqual(requested_params["IsMissing"], "false")
        # Jellyfin omits DateCreated unless it is asked for, and without it
        # every collected episode falls back to its Scrob insert time.
        self.assertIn("DateCreated", requested_params["Fields"])


class JellyfinMovieQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_movies_requests_the_library_add_date(self) -> None:
        requested_params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            requested_params.update(request.url.params)
            return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            movies = await jellyfin.get_movies(
                "library-id", "http://jellyfin.local", "token", "user-id"
            )

        self.assertEqual(movies, [])
        self.assertEqual(requested_params["IncludeItemTypes"], "Movie")
        self.assertIn("DateCreated", requested_params["Fields"])


class JellyfinShowQueryTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for #315: get_shows fetched a single page capped at
    Limit=2000 with no pagination, unlike get_movies/get_episodes - a
    library with more than 2000 series silently dropped the rest, and every
    episode belonging to those shows was skipped too."""

    async def test_get_shows_paginates_past_the_first_page(self) -> None:
        requests: list[dict[str, str]] = []

        total = 1200

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            requests.append(params)
            start = int(params["StartIndex"])
            page = [{"Id": f"show-{start + i}"} for i in range(min(500, total - start))]
            return httpx.Response(200, json={"Items": page, "TotalRecordCount": total})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            shows = await jellyfin.get_shows(
                "library-id", "http://jellyfin.local", "token", "user-id"
            )

        self.assertEqual(len(shows), 1200)
        self.assertEqual(len(requests), 3)
        self.assertEqual([r["StartIndex"] for r in requests], ["0", "500", "1000"])
        self.assertEqual(requests[0]["IncludeItemTypes"], "Series")


class JellyfinSetRatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_rating_preserves_existing_user_data(self) -> None:
        # Regression test for #168: POST .../UserData replaces the whole
        # UserData object, so a rating push that didn't first fetch and merge
        # the existing state would silently reset watched status, playback
        # position, and favorite state back to their defaults.
        requests: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                self.assertEqual(request.url.path, "/Users/user-id/Items/item-id")
                self.assertEqual(request.url.params["Fields"], "UserData")
                return httpx.Response(200, json={
                    "Id": "item-id",
                    "UserData": {
                        "Played": True,
                        "PlayCount": 3,
                        "PlaybackPositionTicks": 12345,
                        "IsFavorite": True,
                        "LastPlayedDate": "2026-08-01T00:00:00.000Z",
                        "Rating": 5.0,
                    },
                })
            requests.append((request.url.path, json.loads(request.content)))
            return httpx.Response(204)

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            ok = await jellyfin.set_rating(
                "http://jellyfin.local", "token", "user-id", "item-id", 8.0
            )

        self.assertTrue(ok)
        self.assertEqual(requests[0][0], "/Users/user-id/Items/item-id/UserData")
        body = requests[0][1]
        # The rating is updated...
        self.assertEqual(body["Rating"], 8.0)
        # ...but everything else from the fetched UserData is carried through
        # unchanged, not reset to defaults.
        self.assertEqual(body["Played"], True)
        self.assertEqual(body["PlayCount"], 3)
        self.assertEqual(body["PlaybackPositionTicks"], 12345)
        self.assertEqual(body["IsFavorite"], True)
        self.assertEqual(body["LastPlayedDate"], "2026-08-01T00:00:00.000Z")

    async def test_set_rating_returns_false_when_fetch_fails(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            ok = await jellyfin.set_rating(
                "http://jellyfin.local", "token", "user-id", "item-id", 8.0
            )

        self.assertFalse(ok)


class JellyfinFindByIdsUserScopedTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #153: the item-detail lookup inside
    find_movie_by_tmdb_id/find_episode_by_ids must hit the user-scoped
    /Users/{id}/Items/{id} endpoint, not the admin-only /Items/{id} one -
    a non-admin token gets a server-side "Guid can't be empty" from Jellyfin
    on the latter."""

    async def test_find_movie_by_tmdb_id_requests_user_scoped_item_detail(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/Items":
                return httpx.Response(200, json={"Items": [{"Id": "movie-item-id", "ProviderIds": {"Tmdb": "550"}}]})
            return httpx.Response(200, json={"Id": "movie-item-id", "Type": "Movie"})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            item = await jellyfin.find_movie_by_tmdb_id(
                "http://jellyfin.local", "token", 550, user_id="user-id"
            )

        self.assertIsNotNone(item)
        self.assertIn("/Users/user-id/Items/movie-item-id", requested_paths)
        self.assertNotIn("/Items/movie-item-id", requested_paths)

    async def test_find_episode_by_ids_requests_user_scoped_item_detail(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/Items" and request.url.params.get("IncludeItemTypes") == "Series":
                return httpx.Response(200, json={"Items": [{"Id": "series-item-id", "ProviderIds": {"Tmdb": "1399"}}]})
            if request.url.path == "/Shows/series-item-id/Episodes":
                return httpx.Response(200, json={"Items": [{"Id": "episode-item-id", "IndexNumber": 1}]})
            return httpx.Response(200, json={"Id": "episode-item-id", "Type": "Episode"})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            item = await jellyfin.find_episode_by_ids(
                "http://jellyfin.local", "token", 1399, 1, 1, user_id="user-id"
            )

        self.assertIsNotNone(item)
        self.assertIn("/Users/user-id/Items/episode-item-id", requested_paths)
        self.assertNotIn("/Items/episode-item-id", requested_paths)

    async def test_find_movie_by_tmdb_id_without_user_id_uses_admin_path(self) -> None:
        # Documents the pre-#153 default: callers that don't pass user_id
        # still get the old (broken-for-non-admin-tokens) behavior - the fix
        # is in threading server_user_id through at every call site, not in
        # this function refusing to run without one.
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/Items":
                return httpx.Response(200, json={"Items": [{"Id": "movie-item-id", "ProviderIds": {"Tmdb": "550"}}]})
            return httpx.Response(200, json={"Id": "movie-item-id"})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            await jellyfin.find_movie_by_tmdb_id("http://jellyfin.local", "token", 550)

        self.assertIn("/Items/movie-item-id", requested_paths)


class JellyfinFindByIdsProviderFilterMismatchTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #247: AnyProviderIdEquals doesn't reliably bind on
    every Jellyfin version, so Items[0] can be an unrelated title. These
    functions must confirm the match against ProviderIds themselves instead
    of trusting the filter, so a push doesn't mark the wrong show/movie
    watched on Jellyfin."""

    async def test_find_movie_by_tmdb_id_skips_a_wrong_first_result(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/Items":
                return httpx.Response(200, json={"Items": [
                    {"Id": "wrong-movie-id", "ProviderIds": {"Tmdb": "9999"}},
                    {"Id": "right-movie-id", "ProviderIds": {"Tmdb": "550"}},
                ]})
            return httpx.Response(200, json={"Id": request.url.path.rsplit("/", 1)[-1]})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            item = await jellyfin.find_movie_by_tmdb_id("http://jellyfin.local", "token", 550)

        self.assertEqual(item["Id"], "right-movie-id")

    async def test_find_movie_by_tmdb_id_returns_none_when_no_result_matches(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"Items": [{"Id": "wrong-movie-id", "ProviderIds": {"Tmdb": "9999"}}]})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            item = await jellyfin.find_movie_by_tmdb_id("http://jellyfin.local", "token", 550)

        self.assertIsNone(item)


class JellyfinItemsBatchTests(unittest.IsolatedAsyncioTestCase):
    """#362: the already-watched check in a full push used to be one
    get_item call per item. get_items_batch/get_items_watched_state replace
    that with Items?Ids= chunks - these confirm the request shape and that a
    missing id is dropped rather than defaulted to unplayed."""

    async def test_requests_user_scoped_path_with_ids_and_userdata_field(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"Items": [
                {"Id": "a", "UserData": {"Played": True}},
                {"Id": "b", "UserData": {"Played": False}},
            ]})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            items = await jellyfin.get_items_batch(
                "http://jellyfin.local", "token", ["a", "b"], user_id="user-id",
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/Users/user-id/Items")
        self.assertEqual(requests[0].url.params["Ids"], "a,b")
        self.assertEqual(requests[0].url.params["Fields"], "UserData")
        self.assertEqual(set(items.keys()), {"a", "b"})

    async def test_admin_path_used_without_a_user_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/Items")
            return httpx.Response(200, json={"Items": []})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            await jellyfin.get_items_batch("http://jellyfin.local", "token", ["a"])

    async def test_reuses_a_passed_in_client_instead_of_opening_a_new_one(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"Items": [{"Id": "a", "UserData": {"Played": True}}]})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not open a new client")),
        ):
            async with _REAL_ASYNC_CLIENT(transport=transport) as shared_client:
                items = await jellyfin.get_items_batch(
                    "http://jellyfin.local", "token", ["a"], client=shared_client,
                )

        self.assertEqual(len(requests), 1)
        self.assertEqual(items["a"]["UserData"]["Played"], True)

    async def test_empty_id_list_short_circuits_without_a_request(self) -> None:
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not make a request")),
        ):
            self.assertEqual(await jellyfin.get_items_batch("http://jellyfin.local", "token", []), {})

    async def test_network_error_returns_empty_dict_not_an_exception(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            items = await jellyfin.get_items_batch("http://jellyfin.local", "token", ["a"])

        self.assertEqual(items, {})

    async def test_watched_state_chunks_past_100_ids_into_multiple_requests(self) -> None:
        seen_id_counts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            ids = request.url.params["Ids"].split(",")
            seen_id_counts.append(len(ids))
            return httpx.Response(200, json={
                "Items": [{"Id": i, "UserData": {"Played": True}} for i in ids],
            })

        transport = httpx.MockTransport(handler)
        item_ids = [f"item-{i}" for i in range(150)]
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            state = await jellyfin.get_items_watched_state("http://jellyfin.local", "token", item_ids)

        self.assertEqual(sorted(seen_id_counts), [50, 100])
        self.assertEqual(len(state), 150)
        self.assertTrue(all(state.values()))

    async def test_watched_state_omits_ids_missing_from_the_server(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # Only "a" still exists on the server - "b" was deleted.
            return httpx.Response(200, json={"Items": [{"Id": "a", "UserData": {"Played": False}}]})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            state = await jellyfin.get_items_watched_state("http://jellyfin.local", "token", ["a", "b"])

        self.assertEqual(state, {"a": False})
        self.assertNotIn("b", state)

    async def test_find_episode_by_ids_skips_a_wrong_first_series_result(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/Items" and request.url.params.get("IncludeItemTypes") == "Series":
                return httpx.Response(200, json={"Items": [
                    {"Id": "wrong-series-id", "ProviderIds": {"Tmdb": "9999"}},
                    {"Id": "right-series-id", "ProviderIds": {"Tmdb": "1399"}},
                ]})
            if request.url.path == "/Shows/right-series-id/Episodes":
                self.assertEqual(request.url.params.get("season"), "1")
                return httpx.Response(200, json={"Items": [{"Id": "episode-id", "IndexNumber": 1}]})
            return httpx.Response(200, json={"Id": "episode-id"})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            item = await jellyfin.find_episode_by_ids("http://jellyfin.local", "token", 1399, 1, 1)

        self.assertEqual(item["Id"], "episode-id")

    async def test_find_episode_by_ids_returns_none_when_the_index_number_is_missing(self) -> None:
        # /Shows/{seriesId}/Episodes is already scoped to the series by path,
        # not a filter param (see find_episode_in_series's docstring - #436),
        # so the only remaining failure mode is no episode at that index.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/Items" and request.url.params.get("IncludeItemTypes") == "Series":
                return httpx.Response(200, json={"Items": [{"Id": "right-series-id", "ProviderIds": {"Tmdb": "1399"}}]})
            if request.url.path == "/Shows/right-series-id/Episodes":
                return httpx.Response(200, json={"Items": [{"Id": "episode-id", "IndexNumber": 2}]})
            return httpx.Response(200, json={"Id": "episode-id"})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            item = await jellyfin.find_episode_by_ids("http://jellyfin.local", "token", 1399, 1, 1)

        self.assertIsNone(item)


if __name__ == "__main__":
    unittest.main()

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
            if request.url.path == "/Items":
                return httpx.Response(200, json={"Items": [{"Id": "episode-item-id", "SeriesId": "series-item-id"}]})
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

    async def test_find_episode_by_ids_skips_a_wrong_first_series_result(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/Items" and request.url.params.get("IncludeItemTypes") == "Series":
                return httpx.Response(200, json={"Items": [
                    {"Id": "wrong-series-id", "ProviderIds": {"Tmdb": "9999"}},
                    {"Id": "right-series-id", "ProviderIds": {"Tmdb": "1399"}},
                ]})
            if request.url.path == "/Items":
                self.assertEqual(request.url.params.get("SeriesId"), "right-series-id")
                return httpx.Response(200, json={"Items": [{"Id": "episode-id", "SeriesId": "right-series-id"}]})
            return httpx.Response(200, json={"Id": "episode-id"})

        transport = httpx.MockTransport(handler)
        with patch.object(
            jellyfin.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            item = await jellyfin.find_episode_by_ids("http://jellyfin.local", "token", 1399, 1, 1)

        self.assertEqual(item["Id"], "episode-id")

    async def test_find_episode_by_ids_rejects_an_episode_from_the_wrong_series(self) -> None:
        # Belt-and-braces: even if the series step is confirmed correct, the
        # episode search result must actually belong to that series.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/Items" and request.url.params.get("IncludeItemTypes") == "Series":
                return httpx.Response(200, json={"Items": [{"Id": "right-series-id", "ProviderIds": {"Tmdb": "1399"}}]})
            if request.url.path == "/Items":
                return httpx.Response(200, json={"Items": [{"Id": "episode-id", "SeriesId": "some-other-series-id"}]})
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

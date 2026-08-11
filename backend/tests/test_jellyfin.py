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


if __name__ == "__main__":
    unittest.main()

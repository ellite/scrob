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


if __name__ == "__main__":
    unittest.main()

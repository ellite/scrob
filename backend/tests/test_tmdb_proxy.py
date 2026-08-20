import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import image_cache, tmdb
from routers import media


class _RecordingAsyncClient:
    def __init__(self, captured: list[dict], **kwargs):
        captured.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, **_kwargs):
        content = b'{"id": 1}' if "/3/" in str(url) else b"image-data"
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "image/jpeg"},
            request=httpx.Request("GET", url),
        )


class _ScalarResult:
    def scalar_one_or_none(self):
        return None


class _NoSettingsDb:
    async def execute(self, _statement):
        return _ScalarResult()


class TmdbProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmdb._cache._store.clear()

    async def test_tmdb_metadata_client_uses_configured_proxy(self) -> None:
        captured: list[dict] = []
        with (
            patch.object(tmdb.settings, "tmdb_proxy_url", "socks5://warp:1080"),
            patch.object(tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _RecordingAsyncClient(captured, **kw)),
        ):
            await tmdb.get_show(1399, api_key="tmdb-token", cache_ttl=None)

        self.assertEqual(captured[0]["proxy"], "socks5://warp:1080")

    async def test_tmdb_metadata_client_is_direct_when_proxy_is_unset(self) -> None:
        captured: list[dict] = []
        with (
            patch.object(tmdb.settings, "tmdb_proxy_url", None),
            patch.object(tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _RecordingAsyncClient(captured, **kw)),
        ):
            await tmdb.get_show(1399, api_key="tmdb-token", cache_ttl=None)

        self.assertNotIn("proxy", captured[0])

    async def test_tmdb_image_client_uses_only_the_configured_tmdb_proxy(self) -> None:
        captured: list[dict] = []
        with (
            patch.object(image_cache.settings, "tmdb_proxy_url", "http://warp:3128"),
            patch.object(image_cache.httpx, "AsyncClient", side_effect=lambda **kw: _RecordingAsyncClient(captured, **kw)),
        ):
            image = await image_cache.fetch_tmdb_image("w500", "/poster.jpg")

        self.assertEqual(image, (b"image-data", "image/jpeg"))
        self.assertEqual(captured[0]["proxy"], "http://warp:3128")

    async def test_tmdb_image_client_is_direct_when_proxy_is_unset(self) -> None:
        captured: list[dict] = []
        with (
            patch.object(image_cache.settings, "tmdb_proxy_url", None),
            patch.object(image_cache.httpx, "AsyncClient", side_effect=lambda **kw: _RecordingAsyncClient(captured, **kw)),
        ):
            await image_cache.fetch_tmdb_image("w500", "/poster.jpg")

        self.assertNotIn("proxy", captured[0])

    async def test_image_endpoint_streams_through_proxy_instead_of_redirecting(self) -> None:
        with (
            patch.object(media.settings, "tmdb_proxy_url", "socks5://warp:1080"),
            patch("core.image_cache.fetch_tmdb_image", new=AsyncMock(return_value=(b"image-data", "image/jpeg"))),
        ):
            response = await media.serve_image("w500", "poster.jpg", _NoSettingsDb(), 1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"image-data")
        self.assertEqual(response.headers["content-type"], "image/jpeg")


if __name__ == "__main__":
    unittest.main()

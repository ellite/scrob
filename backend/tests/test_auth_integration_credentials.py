import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import schemas
from dependencies import get_current_user
from routers import auth


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth.router, prefix="/auth")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    return app


class IntegrationCredentialContractTests(unittest.TestCase):
    def test_sensitive_endpoints_accept_credentials_only_in_request_bodies(self) -> None:
        openapi = _test_app().openapi()
        paths = (
            "/auth/test-tmdb",
            "/auth/test-tvdb",
            "/auth/test-jellyfin",
            "/auth/test-emby",
            "/auth/test-plex",
            "/auth/test-radarr",
            "/auth/radarr/profiles",
            "/auth/test-sonarr",
            "/auth/sonarr/profiles",
        )

        for path in paths:
            with self.subTest(path=path):
                operations = openapi["paths"][path]
                self.assertIn("post", operations)
                self.assertIn("requestBody", operations["post"])
                self.assertNotIn("get", operations)
                query_parameters = {
                    parameter["name"]
                    for parameter in operations["post"].get("parameters", [])
                    if parameter["in"] == "query"
                }
                self.assertTrue(
                    query_parameters.isdisjoint({"key", "url", "token", "user_id"})
                )

    def test_secret_fields_are_redacted_from_model_representations(self) -> None:
        secret = "credential-that-must-not-be-logged"
        api_key_request = schemas.ApiKeyTestRequest(key=secret)
        connection_request = schemas.ServiceConnectionTestRequest(
            url="https://media.example",
            token=secret,
        )

        self.assertNotIn(secret, repr(api_key_request))
        self.assertNotIn(secret, repr(connection_request))


class IntegrationCredentialRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmdb_uses_json_body_and_disables_response_caching(self) -> None:
        app = _test_app()
        transport = httpx.ASGITransport(app=app)
        validate_api_key = AsyncMock(return_value=True)

        with patch("core.tmdb.validate_api_key", validate_api_key):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/auth/test-tmdb",
                    json={"key": "tmdb-secret"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        validate_api_key.assert_awaited_once_with("tmdb-secret")

    async def test_tmdb_rejects_legacy_query_credentials(self) -> None:
        app = _test_app()
        transport = httpx.ASGITransport(app=app)
        validate_api_key = AsyncMock(return_value=True)

        with patch("core.tmdb.validate_api_key", validate_api_key):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/auth/test-tmdb",
                    params={"key": "tmdb-secret"},
                )

        self.assertEqual(response.status_code, 422)
        validate_api_key.assert_not_awaited()

    async def test_tvdb_uses_json_body_and_disables_response_caching(self) -> None:
        app = _test_app()
        transport = httpx.ASGITransport(app=app)
        validate_api_key = AsyncMock(return_value=True)

        with patch("core.tvdb.validate_api_key", validate_api_key):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/auth/test-tvdb",
                    json={"key": "tvdb-secret"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        validate_api_key.assert_awaited_once_with("tvdb-secret")

    async def test_jellyfin_passes_body_credentials_including_user_id(self) -> None:
        app = _test_app()
        transport = httpx.ASGITransport(app=app)
        validate_url = AsyncMock(return_value="https://jellyfin.example")
        validate_connection = AsyncMock(return_value=True)

        with (
            patch.object(auth, "validate_service_url", validate_url),
            patch("core.jellyfin.validate_connection", validate_connection),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/auth/test-jellyfin",
                    json={
                        "url": "https://jellyfin.example/",
                        "token": "jellyfin-secret",
                        "user_id": "user-1",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        validate_connection.assert_awaited_once_with(
            "https://jellyfin.example",
            "jellyfin-secret",
            "user-1",
        )

    async def test_emby_passes_body_credentials_including_user_id(self) -> None:
        app = _test_app()
        transport = httpx.ASGITransport(app=app)
        validate_url = AsyncMock(return_value="https://emby.example")
        validate_connection = AsyncMock(return_value=True)

        with (
            patch.object(auth, "validate_service_url", validate_url),
            patch("core.emby.validate_connection", validate_connection),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/auth/test-emby",
                    json={
                        "url": "https://emby.example/",
                        "token": "emby-secret",
                        "user_id": "user-2",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        validate_connection.assert_awaited_once_with(
            "https://emby.example",
            "emby-secret",
            "user-2",
        )

    async def test_sonarr_test_connection_passes_body_credentials_to_provider(self) -> None:
        app = _test_app()
        transport = httpx.ASGITransport(app=app)
        validate_url = AsyncMock(return_value="https://sonarr.example")
        validate_connection = AsyncMock(return_value=True)

        with (
            patch.object(auth, "validate_service_url", validate_url),
            patch("core.sonarr.validate_connection", validate_connection),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/auth/test-sonarr",
                    json={
                        "url": "https://sonarr.example/",
                        "token": "sonarr-secret",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        validate_connection.assert_awaited_once_with(
            "https://sonarr.example",
            "sonarr-secret",
        )

    async def test_sonarr_profile_discovery_uses_post_body_credentials(self) -> None:
        app = _test_app()
        transport = httpx.ASGITransport(app=app)
        validate_url = AsyncMock(return_value="https://sonarr.example")
        quality_profiles = AsyncMock(return_value=[{"id": 1, "name": "HD"}])
        root_folders = AsyncMock(return_value=[{"path": "/tv"}])
        tags = AsyncMock(return_value=[])

        with (
            patch.object(auth, "validate_service_url", validate_url),
            patch("core.sonarr.get_quality_profiles", quality_profiles),
            patch("core.sonarr.get_root_folders", root_folders),
            patch("core.sonarr.get_tags", tags),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/auth/sonarr/profiles",
                    json={
                        "url": "https://sonarr.example/",
                        "token": "sonarr-secret",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["quality_profiles"][0]["name"], "HD")
        quality_profiles.assert_awaited_once_with(
            "https://sonarr.example",
            "sonarr-secret",
        )
        root_folders.assert_awaited_once_with(
            "https://sonarr.example",
            "sonarr-secret",
        )
        tags.assert_awaited_once_with(
            "https://sonarr.example",
            "sonarr-secret",
        )

    async def test_media_server_test_passes_body_credentials_to_provider(self) -> None:
        app = _test_app()
        transport = httpx.ASGITransport(app=app)
        validate_url = AsyncMock(return_value="https://plex.example")
        validate_connection = AsyncMock(return_value=True)

        with (
            patch.object(auth, "validate_service_url", validate_url),
            patch("core.plex.validate_connection", validate_connection),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/auth/test-plex",
                    json={
                        "url": "https://plex.example/",
                        "token": "plex-secret",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        validate_url.assert_awaited_once_with(
            "https://plex.example/",
            "Plex URL",
        )
        validate_connection.assert_awaited_once_with(
            "https://plex.example",
            "plex-secret",
        )

    async def test_profile_discovery_uses_post_body_credentials(self) -> None:
        app = _test_app()
        transport = httpx.ASGITransport(app=app)
        validate_url = AsyncMock(return_value="https://radarr.example")
        quality_profiles = AsyncMock(return_value=[{"id": 1, "name": "HD"}])
        root_folders = AsyncMock(return_value=[{"path": "/movies"}])
        tags = AsyncMock(return_value=[])

        with (
            patch.object(auth, "validate_service_url", validate_url),
            patch("core.radarr.get_quality_profiles", quality_profiles),
            patch("core.radarr.get_root_folders", root_folders),
            patch("core.radarr.get_tags", tags),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/auth/radarr/profiles",
                    json={
                        "url": "https://radarr.example/",
                        "token": "radarr-secret",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["quality_profiles"][0]["name"], "HD")
        quality_profiles.assert_awaited_once_with(
            "https://radarr.example",
            "radarr-secret",
        )
        root_folders.assert_awaited_once_with(
            "https://radarr.example",
            "radarr-secret",
        )
        tags.assert_awaited_once_with(
            "https://radarr.example",
            "radarr-secret",
        )


if __name__ == "__main__":
    unittest.main()

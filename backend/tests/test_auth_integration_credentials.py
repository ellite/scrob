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


class _SettingsFakeDB:
    """Queues results for db.execute() in call order: the UserSettings lookup,
    then _settings_response's GlobalSettings lookup."""

    def __init__(self, settings):
        self._results = [settings, None]
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def execute(self, stmt):
        item = self._results.pop(0) if self._results else None
        return SimpleNamespace(scalar_one_or_none=lambda: item)

    def add(self, obj):
        pass


class UpdateUserSettingsBingebaseWebhookUrlValidationTests(unittest.IsolatedAsyncioTestCase):
    """Regression test: bingebase_webhook_url is posted to on every playback
    event with no validation at all, unlike every other user-supplied service
    URL (Radarr/Sonarr/Jellyfin/etc.), which all go through validate_service_url
    to block SSRF targets (cloud metadata endpoints, etc.). It needs the same
    treatment - added to update_user_settings' url_fields map alongside
    radarr_url/sonarr_url."""

    async def test_bingebase_webhook_url_is_validated_like_radarr_and_sonarr(self) -> None:
        from models.users import UserSettings

        settings = UserSettings(user_id=1)
        db = _SettingsFakeDB(settings)
        validate_url = AsyncMock(return_value="https://bingebase.example/webhook")

        with patch.object(auth, "validate_service_url", validate_url):
            await auth.update_user_settings(
                schemas.UserSettings(bingebase_webhook_url="https://bingebase.example/webhook/"),
                db=db,
                current_user=SimpleNamespace(id=1),
            )

        validate_url.assert_awaited_once_with(
            "https://bingebase.example/webhook/", "Bingebase Webhook URL",
        )
        self.assertEqual(settings.bingebase_webhook_url, "https://bingebase.example/webhook")

    async def test_ssrf_target_is_rejected(self) -> None:
        from fastapi import HTTPException

        from models.users import UserSettings
        from core.url_validator import validate_service_url

        settings = UserSettings(user_id=1)
        db = _SettingsFakeDB(settings)

        with patch.object(auth, "validate_service_url", validate_service_url):
            with self.assertRaises(HTTPException) as ctx:
                await auth.update_user_settings(
                    schemas.UserSettings(bingebase_webhook_url="http://169.254.169.254/latest/meta-data/"),
                    db=db,
                    current_user=SimpleNamespace(id=1),
                )

        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()

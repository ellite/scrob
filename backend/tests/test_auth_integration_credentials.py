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


class _GlobalSettingsFakeDB:
    """Queues a single result for _settings_response's own GlobalSettings
    lookup (unlike _SettingsFakeDB, which also fronts a UserSettings lookup
    update_user_settings does first - _settings_response takes settings
    directly and only queries once)."""

    def __init__(self, global_settings):
        self._global_settings = global_settings

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._global_settings)


class SettingsResponseEffectiveRadarrSonarrTests(unittest.IsolatedAsyncioTestCase):
    """Regression: the Explore Movies/Shows Radarr/Sonarr filter (#171) was
    shown regardless of whether Radarr/Sonarr was actually configured, since
    nothing on the settings response said either way - unlike TMDB/TVDB,
    which already have has_effective_*_key for exactly this. has_effective_
    radarr/sonarr mirrors _effective_radarr/_effective_sonarr's own "all 4
    fields set, user config first" rule (inlined to avoid a routers.media
    <-> routers.auth cross-import)."""

    def _user_settings(self, **radarr_sonarr_fields):
        from models.users import UserSettings

        return UserSettings(user_id=1, **radarr_sonarr_fields)

    async def test_neither_configured_is_false(self) -> None:
        settings = self._user_settings()
        result = await auth._settings_response(settings, _GlobalSettingsFakeDB(None))
        self.assertFalse(result.has_effective_radarr)
        self.assertFalse(result.has_effective_sonarr)

    async def test_fully_configured_user_radarr_is_true(self) -> None:
        settings = self._user_settings(
            radarr_url="http://radarr.local", radarr_token="tok",
            radarr_root_folder="/movies", radarr_quality_profile=1,
        )
        result = await auth._settings_response(settings, _GlobalSettingsFakeDB(None))
        self.assertTrue(result.has_effective_radarr)
        self.assertFalse(result.has_effective_sonarr)

    async def test_partially_configured_user_radarr_is_false(self) -> None:
        # Missing quality_profile - the "all 4 fields" rule must not treat
        # this as configured just because most fields are set.
        settings = self._user_settings(
            radarr_url="http://radarr.local", radarr_token="tok", radarr_root_folder="/movies",
        )
        result = await auth._settings_response(settings, _GlobalSettingsFakeDB(None))
        self.assertFalse(result.has_effective_radarr)

    async def test_global_only_sonarr_is_true(self) -> None:
        from models.global_settings import GlobalSettings

        settings = self._user_settings()
        gs = GlobalSettings(
            id=1, sonarr_url="http://sonarr.local", sonarr_token="tok",
            sonarr_root_folder="/tv", sonarr_quality_profile=1,
        )
        result = await auth._settings_response(settings, _GlobalSettingsFakeDB(gs))
        self.assertTrue(result.has_effective_sonarr)
        self.assertFalse(result.has_effective_radarr)

    async def test_user_config_does_not_need_global_settings_row_to_exist(self) -> None:
        # gs is None (no GlobalSettings row at all) - must not crash on the
        # `gs and all([...])` short-circuit when checking the global side.
        settings = self._user_settings(
            sonarr_url="http://sonarr.local", sonarr_token="tok",
            sonarr_root_folder="/tv", sonarr_quality_profile=1,
        )
        result = await auth._settings_response(settings, _GlobalSettingsFakeDB(None))
        self.assertTrue(result.has_effective_sonarr)


if __name__ == "__main__":
    unittest.main()

"""Fixtures for the live-integration suite.

Unlike tests/, these hit real, running Plex/Emby/Jellyfin dev containers
(managed via DevHub) and the real scrob-dev backend - no mocks. They exist to
prove a specific fix actually works against a real server's API, not for
routine/CI runs.

Skipped automatically (collection-time, not per-test) unless
backend/.env.test.local (gitignored) provides SCROB_TEST_* credentials -
safe to leave in a shared checkout or CI without ever running by accident.

Invoke explicitly: `pytest tests_live/ -v` (never picked up by a bare
`pytest` from backend/, since that only discovers tests/ by convention here -
but keep it that way, don't add tests_live to testpaths).
"""
import asyncio
import os
import time
from pathlib import Path

import asyncpg
import httpx
import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env.test.local")

SCROB_BASE_URL = os.environ.get("SCROB_TEST_BASE_URL", "")
SCROB_USERNAME = os.environ.get("SCROB_TEST_USERNAME", "")
SCROB_PASSWORD = os.environ.get("SCROB_TEST_PASSWORD", "")
DEVHUB_BASE_URL = os.environ.get("DEVHUB_BASE_URL", "")
DEVHUB_API_TOKEN = os.environ.get("DEVHUB_API_TOKEN", "")
DB_HOST = os.environ.get("SCROB_TEST_DB_HOST", "")
DB_PORT = os.environ.get("SCROB_TEST_DB_PORT", "")
DB_USER = os.environ.get("SCROB_TEST_DB_USER", "")
DB_PASSWORD = os.environ.get("SCROB_TEST_DB_PASSWORD", "")
DB_NAME = os.environ.get("SCROB_TEST_DB_NAME", "")

_MISSING = not all([SCROB_BASE_URL, SCROB_USERNAME, SCROB_PASSWORD, DEVHUB_BASE_URL, DEVHUB_API_TOKEN])
_DB_MISSING = not all([DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME])


def pytest_collection_modifyitems(config, items):
    if not _MISSING:
        return
    skip = pytest.mark.skip(reason="live-integration credentials not configured (backend/.env.test.local)")
    for item in items:
        if "tests_live" in str(item.fspath):
            item.add_marker(skip)


class DevHub:
    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=DEVHUB_BASE_URL,
            headers={"Authorization": f"Bearer {DEVHUB_API_TOKEN}"},
            timeout=15.0,
        )

    def status(self, service_id: str) -> dict:
        r = self._client.get(f"/api/services/{service_id}")
        r.raise_for_status()
        return r.json()

    def ensure_running(self, service_id: str, timeout: float = 60.0) -> dict:
        """Starts the service if it isn't already, and blocks until it's up.

        Start is fire-and-forget on DevHub's side (see API.md) - the poll
        loop here is what actually waits.
        """
        snap = self.status(service_id)
        if snap["status"] == "running":
            return snap
        r = self._client.post(f"/api/services/{service_id}/start")
        r.raise_for_status()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = self.status(service_id)
            if snap["status"] == "running":
                return snap
            if snap["status"] in ("crashed", "error"):
                raise RuntimeError(f"{service_id} failed to start: {snap}")
            time.sleep(1.0)
        raise TimeoutError(f"{service_id} did not reach 'running' within {timeout}s (last: {snap})")


@pytest.fixture(scope="session")
def devhub() -> DevHub:
    return DevHub()


@pytest.fixture(scope="session")
def scrob_token() -> str:
    r = httpx.post(
        f"{SCROB_BASE_URL}/auth/login",
        data={"username": SCROB_USERNAME, "password": SCROB_PASSWORD},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def scrob_api(scrob_token: str) -> httpx.Client:
    """Authenticated client against the scrob-dev backend under test."""
    client = httpx.Client(
        base_url=SCROB_BASE_URL,
        headers={"Authorization": f"Bearer {scrob_token}"},
        timeout=30.0,
    )
    yield client
    client.close()


@pytest.fixture(scope="session")
def scrob_connections(scrob_api: httpx.Client) -> dict:
    """The test account's configured media-server connections, keyed by type.

    Reading these live from Scrob's own /auth/connections (rather than
    hardcoding server tokens in the env file) means the suite always tests
    against whatever's actually configured - if the dev containers get
    reconfigured, there's nothing stale to fix here.
    """
    r = scrob_api.get("/auth/connections")
    r.raise_for_status()
    by_type = {}
    for conn in r.json():
        by_type.setdefault(conn["type"], conn)
    return by_type


def _jellyfin_client(conn: dict) -> httpx.Client:
    return httpx.Client(
        base_url=conn["url"],
        headers={"Authorization": f'MediaBrowser Token="{conn["token"]}"'},
        timeout=15.0,
    )


@pytest.fixture(scope="session")
def jellyfin_conn(devhub: DevHub, scrob_connections: dict) -> dict:
    devhub.ensure_running(os.environ.get("DEVHUB_JELLYFIN_SERVICE_ID", "jellyfin-dev"))
    if "jellyfin" not in scrob_connections:
        pytest.skip("no jellyfin connection configured on the scrob test account")
    return scrob_connections["jellyfin"]


@pytest.fixture(scope="session")
def jellyfin_client(jellyfin_conn: dict) -> httpx.Client:
    client = _jellyfin_client(jellyfin_conn)
    yield client
    client.close()


@pytest.fixture(scope="session")
def emby_conn(devhub: DevHub, scrob_connections: dict) -> dict:
    devhub.ensure_running(os.environ.get("DEVHUB_EMBY_SERVICE_ID", "emby-dev"))
    if "emby" not in scrob_connections:
        pytest.skip("no emby connection configured on the scrob test account")
    return scrob_connections["emby"]


@pytest.fixture(scope="session")
def emby_client(emby_conn: dict) -> httpx.Client:
    client = _jellyfin_client(emby_conn)  # same REST API/auth scheme as Jellyfin
    yield client
    client.close()


@pytest.fixture(scope="session")
def plex_conn(devhub: DevHub, scrob_connections: dict) -> dict:
    devhub.ensure_running(os.environ.get("DEVHUB_PLEX_SERVICE_ID", "plex-dev"))
    if "plex" not in scrob_connections:
        pytest.skip("no plex connection configured on the scrob test account")
    return scrob_connections["plex"]


@pytest.fixture(scope="session")
def plex_client(plex_conn: dict) -> httpx.Client:
    client = httpx.Client(
        base_url=plex_conn["url"],
        headers={"X-Plex-Token": plex_conn["token"], "Accept": "application/json"},
        timeout=15.0,
    )
    yield client
    client.close()


class DevDB:
    """Sync-friendly wrapper around asyncpg, so tests don't need to be async
    themselves (this suite otherwise stays on plain sync httpx calls).

    Opens a fresh connection per call rather than caching one - an asyncpg
    connection is bound to the event loop that created it, and each
    asyncio.run() here gets a new loop.
    """

    @staticmethod
    def fetchrow(query: str, *args):
        async def _run():
            conn = await asyncpg.connect(host=DB_HOST, port=int(DB_PORT), user=DB_USER, password=DB_PASSWORD, database=DB_NAME)
            try:
                return await conn.fetchrow(query, *args)
            finally:
                await conn.close()
        return asyncio.run(_run())

    @staticmethod
    def execute(query: str, *args):
        async def _run():
            conn = await asyncpg.connect(host=DB_HOST, port=int(DB_PORT), user=DB_USER, password=DB_PASSWORD, database=DB_NAME)
            try:
                return await conn.execute(query, *args)
            finally:
                await conn.close()
        return asyncio.run(_run())


@pytest.fixture
def dev_db():
    """Direct connection to the scrob-dev Postgres, for tests that need to
    fabricate DB state (e.g. a TVDB-only show) that can't be reached through
    Scrob's own API. Use sparingly and always restore what you change -
    this is the same dev DB every other fixture/test reads through the API."""
    if _DB_MISSING:
        pytest.skip("live-integration DB credentials not configured (backend/.env.test.local)")
    return DevDB()

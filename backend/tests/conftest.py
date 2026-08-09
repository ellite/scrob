"""Test-suite bootstrap.

core.config.Settings requires SECRET_KEY, and db.py builds an engine from
DATABASE_URL at import time, so both must exist before any test module is
imported. Most test modules set them themselves, but test_episode_order,
test_jellyfin and test_plex do not — they pass today only because pytest
collects alphabetically and some earlier module happens to set them first.

Setting them here makes any single test file runnable on its own and makes CI
independent of collection order. The per-module setdefault calls that already
exist stay harmless no-ops.

Note the URL is never connected to: create_async_engine is lazy, and no test
opens a connection.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

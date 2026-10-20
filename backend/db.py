from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from core.config import settings
from core.timeutil import utcnow

engine = create_async_engine(
    settings.db_url,
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
    # Every timestamp column in this app is naive (DateTime, no timezone) and
    # either defaults via Postgres's own func.now() or Python's
    # utcnow() - both are only consistent with each other, and with
    # the frontend's "treat naive timestamps as UTC" assumption, if the
    # database session's own timezone is UTC. Without pinning it here,
    # Postgres falls back to whatever timezone the container's TZ env var
    # (or the server's own config) happens to set, so func.now() silently
    # stores local wall-clock time into what everything else treats as UTC -
    # e.g. a job that ran at 8:36am AEST would be stored as "18:36" and then
    # displayed as if that were already UTC, showing hours in the future.
    connect_args={"server_settings": {"timezone": "UTC"}},
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
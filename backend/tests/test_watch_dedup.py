from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.watch_dedup import find_duplicate_watch_event


@pytest.mark.asyncio
async def test_duplicate_check_only_matches_completed_rows():
    # #481: an in-progress row (completed=False) from a Jellyfin/Emby pull
    # must not swallow the real completion arriving from a webhook.
    db = AsyncMock()
    db.get_bind = MagicMock(return_value=None)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result

    await find_duplicate_watch_event(db, 1, 2, datetime(2026, 10, 16, 7, 28), 20)

    statement = db.execute.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "watch_events.completed is true" in compiled or "watch_events.completed is 1" in compiled

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime.

    Timestamps are stored without tzinfo (TIMESTAMP WITHOUT TIME ZONE), so this
    keeps datetime.utcnow()'s naive result without its Python 3.12 deprecation.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)

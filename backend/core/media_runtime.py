"""One definition of a Media row's runtime for SQL aggregates (watch time,
average runtime). Episodes often carry their runtime only in
tmdb_data['runtime'], while movies use the Media.runtime column."""

from sqlalchemy import func
from sqlalchemy.types import Integer as SAInteger, Text as SAText

from models.media import Media


def effective_runtime_expression():
    """Media.runtime, falling back to the cached tmdb_data runtime.

    tmdb_data is a JSON (not JSONB) column, which has no .astext: it is cast to
    text first, and a JSON null must be cleared (NULLIF) before the integer
    cast. The in-Python counterpart for a single row is
    routers.history._effective_runtime."""
    json_runtime = func.cast(
        func.nullif(func.cast(Media.tmdb_data["runtime"], SAText), "null"),
        SAInteger,
    )
    return func.coalesce(Media.runtime, json_runtime)

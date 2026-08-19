import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from models.collection import Collection
from models.media import Media
from routers.media import recently_added_order


def _rendered_order_by():
    max_added = (
        select(Collection.media_id, func.max(Collection.added_at).label("max_added"))
        .group_by(Collection.media_id)
        .subquery()
        .c.max_added
    )
    query = select(Media).order_by(*recently_added_order(max_added))
    return str(query.compile(dialect=postgresql.dialect())).split("ORDER BY")[1]


class RecentlyAddedOrderTests(unittest.TestCase):
    """Media servers stamp a whole batch of files with the same second, so the
    add-date alone leaves Postgres free to return a different arrangement on
    every request - which is how a season ends up shuffled on the rail."""

    def test_add_date_leads(self):
        # The subquery column renders with its generated alias prefix.
        first_key = _rendered_order_by().split(",")[0].strip()
        self.assertTrue(first_key.endswith("max_added DESC"), first_key)

    def test_every_remaining_key_is_present_and_descending(self):
        rendered = _rendered_order_by()
        for column in ("media.show_id", "media.season_number", "media.episode_number", "media.id"):
            self.assertIn(f"{column} DESC", rendered)

    def test_a_shows_episodes_group_before_they_are_ordered(self):
        rendered = _rendered_order_by()
        self.assertLess(rendered.index("media.show_id"), rendered.index("media.season_number"))
        self.assertLess(rendered.index("media.season_number"), rendered.index("media.episode_number"))

    def test_media_id_is_the_final_key_so_the_order_is_total(self):
        keys = [part.strip() for part in _rendered_order_by().split(",")]
        self.assertTrue(keys[-1].startswith("media.id"))

    def test_nullable_episode_columns_sort_last(self):
        # A DESC sort puts NULLs first in Postgres, which would float every
        # movie above the episodes sharing its timestamp.
        rendered = _rendered_order_by()
        for column in ("media.show_id", "media.season_number", "media.episode_number"):
            self.assertIn(f"{column} DESC NULLS LAST", rendered)


if __name__ == "__main__":
    unittest.main()

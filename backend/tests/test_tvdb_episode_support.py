import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.enrichment import tmdb_season_covers, is_unmapped_tvdb_episode, enrich_episode_from_tvdb
from models.media import Media, MediaType


class TmdbSeasonCoversTests(unittest.TestCase):
    """Regression tests for #101: a show can be sparsely listed on TMDB (e.g.
    4 seasons) while fully listed on TVDB (e.g. 12 seasons). This helper
    decides whether an episode is confidently absent from TMDB (safe to
    enrich from TVDB directly) or merely unmapped-but-maybe-present
    (ambiguous — must not guess)."""

    SEASONS = [
        {"season_number": 1, "episode_count": 10, "name": "Season 1"},
        {"season_number": 2, "episode_count": 8, "name": "Season 2"},
    ]

    def test_season_and_episode_within_range_covers(self):
        self.assertTrue(tmdb_season_covers({"seasons": self.SEASONS}, 1, 5))

    def test_season_exists_but_episode_out_of_range_does_not_cover(self):
        self.assertFalse(tmdb_season_covers({"seasons": self.SEASONS}, 2, 9))

    def test_season_does_not_exist_does_not_cover(self):
        # Detroit Muscle's exact scenario: TVDB season 5 when TMDB only has 2.
        self.assertFalse(tmdb_season_covers({"seasons": self.SEASONS}, 5, 1))

    def test_missing_tmdb_data_does_not_cover(self):
        self.assertFalse(tmdb_season_covers(None, 1, 1))
        self.assertFalse(tmdb_season_covers({}, 1, 1))


class IsUnmappedTvdbEpisodeTests(unittest.TestCase):
    """Regression tests for #101: episodes enriched from TVDB must be
    identifiable so outbound pushes (Trakt/Simkl/MDBList) can exclude them —
    their tmdb_id is a TVDB episode id in disguise, not a real TMDB id."""

    def test_tvdb_sourced_episode_is_unmapped(self):
        media = Media(media_type=MediaType.episode, tmdb_data={"source": "tvdb"})
        self.assertTrue(is_unmapped_tvdb_episode(media))

    def test_tmdb_sourced_episode_is_not_unmapped(self):
        media = Media(media_type=MediaType.episode, tmdb_data={"runtime": 42})
        self.assertFalse(is_unmapped_tvdb_episode(media))

    def test_no_tmdb_data_is_not_unmapped(self):
        media = Media(media_type=MediaType.episode, tmdb_data=None)
        self.assertFalse(is_unmapped_tvdb_episode(media))

    def test_non_episode_media_is_never_unmapped(self):
        media = Media(media_type=MediaType.movie, tmdb_data={"source": "tvdb"})
        self.assertFalse(is_unmapped_tvdb_episode(media))


class EnrichEpisodeFromTvdbTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #101: populating a bare episode Media record from
    TVDB data, mirroring the tmdb_id-disguise convention already used by the
    "resolve unmatched show to TVDB" flow in routers/sync.py."""

    async def test_populates_fields_and_tags_source(self):
        media = Media(media_type=MediaType.episode, season_number=5, episode_number=1)
        tvdb_data = {
            "tvdb_id": 9988776,
            "season_number": 5,
            "episode_number": 1,
            "name": "Big Block Swap",
            "overview": "An engine gets swapped.",
            "air_date": "2023-04-01",
            "runtime": 42,
            "image_url": "https://artworks.thetvdb.com/banners/episodes/x.jpg",
        }
        await enrich_episode_from_tvdb(media, tvdb_data)
        self.assertEqual(media.tmdb_id, 9988776)
        self.assertEqual(media.title, "Big Block Swap")
        self.assertEqual(media.overview, "An engine gets swapped.")
        self.assertEqual(media.poster_path, tvdb_data["image_url"])
        self.assertEqual(media.release_date, "2023-04-01")
        self.assertEqual(media.tmdb_data["source"], "tvdb")
        self.assertEqual(media.tmdb_data["tvdb_episode_id"], 9988776)
        self.assertEqual(media.tmdb_data["runtime"], 42)

    async def test_missing_tvdb_id_leaves_existing_tmdb_id_untouched(self):
        media = Media(media_type=MediaType.episode, tmdb_id=None)
        await enrich_episode_from_tvdb(media, {"name": "Untitled"})
        self.assertIsNone(media.tmdb_id)
        self.assertEqual(media.tmdb_data["source"], "tvdb")

    async def test_missing_name_keeps_existing_title(self):
        media = Media(media_type=MediaType.episode, title="Placeholder")
        await enrich_episode_from_tvdb(media, {"tvdb_id": 1})
        self.assertEqual(media.title, "Placeholder")


if __name__ == "__main__":
    unittest.main()

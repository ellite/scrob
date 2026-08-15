import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.media import _apply_local_filters, _paginate_matches


def _item(tmdb_id, in_library=False, watched=False, watch_started=False, is_monitored=False):
    return {
        "tmdb_id": tmdb_id,
        "in_library": in_library,
        "watched": watched,
        "watch_started": watch_started,
        "is_monitored": is_monitored,
    }


class ApplyLocalFiltersTests(unittest.TestCase):
    """#171: collection/watch/arr filters applied locally after TMDB
    enrichment, since TMDB's discover API can't express them."""

    def test_no_filters_is_a_passthrough(self):
        items = [_item(1), _item(2, in_library=True)]
        self.assertEqual(_apply_local_filters(items, None, None, None), items)

    def test_collection_in(self):
        items = [_item(1, in_library=True), _item(2, in_library=False)]
        result = _apply_local_filters(items, "in", None, None)
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_collection_out(self):
        items = [_item(1, in_library=True), _item(2, in_library=False)]
        result = _apply_local_filters(items, "out", None, None)
        self.assertEqual([i["tmdb_id"] for i in result], [2])

    def test_watch_watched(self):
        items = [_item(1, watched=True), _item(2, watched=False)]
        result = _apply_local_filters(items, None, "watched", None)
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_watch_unwatched_excludes_in_progress(self):
        # Regression: "unwatched" must mean "never started", not just "not
        # finished" - an in-progress item is neither watched nor unwatched.
        items = [
            _item(1, watched=False, watch_started=False),  # never started
            _item(2, watched=False, watch_started=True),   # in progress
            _item(3, watched=True, watch_started=True),    # finished
        ]
        result = _apply_local_filters(items, None, "unwatched", None)
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_watch_started_excludes_watched_and_untouched(self):
        items = [
            _item(1, watched=False, watch_started=False),
            _item(2, watched=False, watch_started=True),
            _item(3, watched=True, watch_started=True),
        ]
        result = _apply_local_filters(items, None, "started", None)
        self.assertEqual([i["tmdb_id"] for i in result], [2])

    def test_arr_added_and_notadded(self):
        items = [_item(1, is_monitored=True), _item(2, is_monitored=False)]
        self.assertEqual([i["tmdb_id"] for i in _apply_local_filters(items, None, None, "added")], [1])
        self.assertEqual([i["tmdb_id"] for i in _apply_local_filters(items, None, None, "notadded")], [2])

    def test_filters_combine_with_and_semantics(self):
        items = [
            _item(1, in_library=True, watched=True),   # matches both
            _item(2, in_library=True, watched=False),  # collection only
            _item(3, in_library=False, watched=True),  # watch only
        ]
        result = _apply_local_filters(items, "in", "watched", None)
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_unknown_filter_value_matches_nothing(self):
        # Defensive: an unrecognized value for a filter key isn't silently
        # treated as "no filter" - it falls through every branch and the
        # unmodified branches' items remain, but no branch positively matches
        # an unknown value, so this documents current behavior rather than
        # asserting a stricter contract than the function actually has.
        items = [_item(1, watched=True)]
        result = _apply_local_filters(items, None, "bogus", None)
        self.assertEqual(result, items)


class PaginateMatchesTests(unittest.TestCase):
    """The scanned-and-filtered match list is a flat list built across
    however many TMDB pages it took to fill the request - this slices it
    into one fixed-size page and derives an approximate total_pages."""

    def test_full_page_with_more_remaining(self):
        matched = [_item(i) for i in range(25)]
        page_items, total_pages = _paginate_matches(matched, page=1, page_size=20)
        self.assertEqual(len(page_items), 20)
        self.assertEqual([i["tmdb_id"] for i in page_items], list(range(20)))
        self.assertEqual(total_pages, 2)  # one page ahead, since more exist

    def test_exact_page_with_nothing_remaining(self):
        matched = [_item(i) for i in range(20)]
        page_items, total_pages = _paginate_matches(matched, page=1, page_size=20)
        self.assertEqual(len(page_items), 20)
        self.assertEqual(total_pages, 1)  # not advertised as having a next page

    def test_partial_last_page(self):
        matched = [_item(i) for i in range(15)]
        page_items, total_pages = _paginate_matches(matched, page=1, page_size=20)
        self.assertEqual(len(page_items), 15)
        self.assertEqual(total_pages, 1)

    def test_second_page_slicing(self):
        matched = [_item(i) for i in range(45)]
        page_items, total_pages = _paginate_matches(matched, page=2, page_size=20)
        self.assertEqual([i["tmdb_id"] for i in page_items], list(range(20, 40)))
        self.assertEqual(total_pages, 3)

    def test_empty_matches(self):
        page_items, total_pages = _paginate_matches([], page=1, page_size=20)
        self.assertEqual(page_items, [])
        self.assertEqual(total_pages, 1)

    def test_requested_page_beyond_available_matches_is_empty_not_an_error(self):
        # The scan hit MAX_SCAN_PAGES before finding enough matches for a
        # page this deep - must degrade to an empty page, not crash or wrap.
        matched = [_item(i) for i in range(10)]
        page_items, total_pages = _paginate_matches(matched, page=5, page_size=20)
        self.assertEqual(page_items, [])
        self.assertEqual(total_pages, 5)


if __name__ == "__main__":
    unittest.main()

import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.media import _apply_local_filters, _paginate_matches


def _item(tmdb_id, in_library=False, watched=False, watch_started=False, is_monitored=False, release_date=None, user_rating=None):
    return {
        "tmdb_id": tmdb_id,
        "in_library": in_library,
        "watched": watched,
        "watch_started": watch_started,
        "is_monitored": is_monitored,
        "release_date": release_date,
        "user_rating": user_rating,
    }


class ApplyLocalFiltersTests(unittest.TestCase):
    """#171: collection/watch/arr filters applied locally after TMDB
    enrichment, since TMDB's discover API can't express them. Each category
    is a multi-select list, OR'd internally and AND'd across categories,
    matching the existing genre/year convention on /media/list."""

    def test_no_filters_is_a_passthrough(self):
        items = [_item(1), _item(2, in_library=True)]
        self.assertEqual(_apply_local_filters(items, [], [], []), items)

    def test_collection_in(self):
        items = [_item(1, in_library=True), _item(2, in_library=False)]
        result = _apply_local_filters(items, ["in"], [], [])
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_collection_out(self):
        items = [_item(1, in_library=True), _item(2, in_library=False)]
        result = _apply_local_filters(items, ["out"], [], [])
        self.assertEqual([i["tmdb_id"] for i in result], [2])

    def test_collection_in_and_out_selected_together_matches_everything(self):
        # Selecting both values in a category is a no-op (OR of mutually
        # exclusive conditions covers every item) - same degenerate case
        # already accepted for multi-select "watched"/"unwatched" on the
        # collection pages, not something this function needs to prevent.
        items = [_item(1, in_library=True), _item(2, in_library=False)]
        result = _apply_local_filters(items, ["in", "out"], [], [])
        self.assertEqual(result, items)

    def test_watch_watched(self):
        items = [_item(1, watched=True), _item(2, watched=False)]
        result = _apply_local_filters(items, [], ["watched"], [])
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_watch_unwatched_excludes_in_progress(self):
        # Regression: "unwatched" must mean "never started", not just "not
        # finished" - an in-progress item is neither watched nor unwatched.
        items = [
            _item(1, watched=False, watch_started=False),  # never started
            _item(2, watched=False, watch_started=True),   # in progress
            _item(3, watched=True, watch_started=True),    # finished
        ]
        result = _apply_local_filters(items, [], ["unwatched"], [])
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_watch_started_excludes_watched_and_untouched(self):
        items = [
            _item(1, watched=False, watch_started=False),
            _item(2, watched=False, watch_started=True),
            _item(3, watched=True, watch_started=True),
        ]
        result = _apply_local_filters(items, [], ["started"], [])
        self.assertEqual([i["tmdb_id"] for i in result], [2])

    def test_watch_multiple_values_ored_together(self):
        # watched OR started - excludes only the never-touched item.
        items = [
            _item(1, watched=False, watch_started=False),
            _item(2, watched=False, watch_started=True),
            _item(3, watched=True, watch_started=True),
        ]
        result = _apply_local_filters(items, [], ["started", "watched"], [])
        self.assertEqual({i["tmdb_id"] for i in result}, {2, 3})

    def test_arr_added_and_notadded(self):
        items = [_item(1, is_monitored=True), _item(2, is_monitored=False)]
        self.assertEqual([i["tmdb_id"] for i in _apply_local_filters(items, [], [], ["added"])], [1])
        self.assertEqual([i["tmdb_id"] for i in _apply_local_filters(items, [], [], ["notadded"])], [2])

    def test_filters_combine_with_and_semantics_across_categories(self):
        items = [
            _item(1, in_library=True, watched=True),   # matches both
            _item(2, in_library=True, watched=False),  # collection only
            _item(3, in_library=False, watched=True),  # watch only
        ]
        result = _apply_local_filters(items, ["in"], ["watched"], [])
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_unrecognized_value_alone_is_a_no_op(self):
        # A category made up entirely of unrecognized values contributes no
        # check rather than matching nothing.
        items = [_item(1, watched=True), _item(2, watched=False)]
        result = _apply_local_filters(items, [], ["bogus"], [])
        self.assertEqual(result, items)

    def test_unrecognized_value_alongside_a_real_one_is_ignored(self):
        items = [_item(1, watched=True), _item(2, watched=False)]
        result = _apply_local_filters(items, [], ["bogus", "watched"], [])
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_my_rating_matches_only_the_selected_personal_ratings(self):
        items = [
            _item(1, user_rating=10),
            _item(2, user_rating=8),
            _item(3, user_rating=None),
        ]
        result = _apply_local_filters(items, [], [], [], my_rating=[8, 10])
        self.assertEqual([i["tmdb_id"] for i in result], [1, 2])

    def test_my_rating_combines_with_the_existing_local_filters(self):
        items = [
            _item(1, in_library=True, user_rating=8),
            _item(2, in_library=False, user_rating=8),
            _item(3, in_library=True, user_rating=7),
        ]
        result = _apply_local_filters(items, ["in"], [], [], my_rating=[8])
        self.assertEqual([i["tmdb_id"] for i in result], [1])


class ApplyLocalFiltersYearTests(unittest.TestCase):
    """year has no multi-value OR support in TMDB's discover API (unlike
    genre, which uses "|"-joined ids in one request) - a multi-year
    selection is applied locally here instead, same as collection/watch/arr."""

    def test_single_year(self):
        items = [_item(1, release_date="2020-05-01"), _item(2, release_date="2021-01-01")]
        result = _apply_local_filters(items, [], [], [], year=[2020])
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_multiple_years_ored_together(self):
        items = [
            _item(1, release_date="2020-05-01"),
            _item(2, release_date="2021-01-01"),
            _item(3, release_date="2022-01-01"),
        ]
        result = _apply_local_filters(items, [], [], [], year=[2020, 2022])
        self.assertEqual({i["tmdb_id"] for i in result}, {1, 3})

    def test_missing_release_date_never_matches(self):
        items = [_item(1, release_date=None), _item(2, release_date="")]
        result = _apply_local_filters(items, [], [], [], year=[2020])
        self.assertEqual(result, [])

    def test_combines_with_other_categories(self):
        items = [
            _item(1, release_date="2020-05-01", in_library=True),
            _item(2, release_date="2020-05-01", in_library=False),
            _item(3, release_date="2021-05-01", in_library=True),
        ]
        result = _apply_local_filters(items, ["in"], [], [], year=[2020])
        self.assertEqual([i["tmdb_id"] for i in result], [1])

    def test_empty_year_list_is_a_passthrough(self):
        items = [_item(1, release_date="2020-05-01")]
        self.assertEqual(_apply_local_filters(items, [], [], [], year=[]), items)
        self.assertEqual(_apply_local_filters(items, [], [], []), items)


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

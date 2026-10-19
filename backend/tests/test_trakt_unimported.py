import logging

from routers import trakt


def test_unimported_items_are_listed_in_the_job_summary(caplog):
    # #454: a Trakt import that couldn't bring some items in should say which.
    stats = {"movies": 1, "episodes": 0, "ratings": 0, "lists": 0, "list_items": 0, "skipped": 1, "errors": 1}
    trakt._note_unimported(stats, 'movie "Old Film"', "no TMDB id")
    trakt._note_unimported(stats, 'show "Poirot" S1E1', "episode not found on TMDB")

    with caplog.at_level(logging.WARNING, logger=trakt.logger.name):
        trakt._trakt_import_summary(7, "export import", stats)

    assert 'movie "Old Film" (no TMDB id)' in caplog.text
    assert 'show "Poirot" S1E1 (episode not found on TMDB)' in caplog.text


def test_unimported_list_is_capped():
    stats: dict = {}
    for i in range(trakt._UNIMPORTED_CAP + 5):
        trakt._note_unimported(stats, f"item {i}", "x")
    assert len(stats["unimported"]) == trakt._UNIMPORTED_CAP
    assert stats["unimported_more"] == 5

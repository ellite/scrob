"""Live regression test for GitHub #437 (Jellyfin/Emby side only - Plex has
no timestamp field on /:/scrobble, see the issue thread and
backend/routers/sync.py's _push_plex_watched_and_record).

Marks the dev fixture's "Se7en" as watched in Scrob with an explicit
watched_at standing in for a user-selected Air Date, then reads the watched
timestamp back directly from Jellyfin's own API and asserts it matches -
not "now". This is exactly the bug reporter's repro (#437's Steps to
reproduce): before the fix in history.py/_push_watch_state and sync.py's
push loops, Jellyfin always showed the push moment instead.
"""
import httpx

from tests_live.helpers import find_media_by_tmdb_id, trigger_and_wait_for_sync

SE7EN_TMDB_ID = 807
SE7EN_AIR_DATE = "1995-09-22"
EXPECTED_WATCHED_AT = f"{SE7EN_AIR_DATE}T12:00:00"  # noon UTC - see Base.astro's air-date convention


def test_air_date_reaches_jellyfin_as_last_played_date(
    scrob_api: httpx.Client, jellyfin_client: httpx.Client, jellyfin_conn: dict
):
    trigger_and_wait_for_sync(scrob_api, "jellyfin")

    media = find_media_by_tmdb_id(scrob_api, "movie", SE7EN_TMDB_ID)
    assert media is not None, "Se7en not found in Scrob's collection - did the Jellyfin dev fixture change?"

    r = jellyfin_client.get(
        "/Items",
        params={"IncludeItemTypes": "Movie", "Recursive": "true", "Fields": "ProviderIds"},
    )
    r.raise_for_status()
    jf_item = next(
        (i for i in r.json()["Items"] if i.get("ProviderIds", {}).get("Tmdb") == str(SE7EN_TMDB_ID)),
        None,
    )
    assert jf_item is not None, "Se7en not found on the Jellyfin dev server"

    resp = scrob_api.post(
        "/history",
        json={
            "tmdb_id": SE7EN_TMDB_ID,
            "media_type": "movie",
            "watched_at": EXPECTED_WATCHED_AT,
            "completed": True,
            "force": True,  # already watched from a previous run of this test - re-mark anyway (#390 dedup guard)
        },
    )
    resp.raise_for_status()

    r = jellyfin_client.get(
        f"/Users/{jellyfin_conn['server_user_id']}/Items/{jf_item['Id']}",
        params={"Fields": "UserData"},
    )
    r.raise_for_status()
    user_data = r.json()["UserData"]

    assert user_data["Played"] is True
    assert user_data["LastPlayedDate"].startswith(EXPECTED_WATCHED_AT), (
        f"expected Jellyfin's LastPlayedDate to reflect the air date {EXPECTED_WATCHED_AT}, "
        f"got {user_data['LastPlayedDate']} (looks like it was stamped with the push time instead - #437 regression)"
    )

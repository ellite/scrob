"""Live end-to-end test for GitHub #436 (Test B - see
test_436_jellyfin_tvdb_index.py for the narrower, DB-free version).

Simulates a show Scrob only ever matched via TVDB (Show.tmdb_id = NULL) -
e.g. one added through a legacy-agent Plex library, see plex.get_guids -
by nulling it directly in the dev DB and deleting the cached
collection_files row for one of its episodes on the Jellyfin connection, so
the real full-push job (_run_full_push, POST /sync/connection/{id}/push)
has to fall through to the live _find_source_id lookup this fix touches
instead of hitting its fast, already-cached-source_id path.

Both mutations are restored in a finally block. This state doesn't occur
naturally for the dev fixture's shows (Jellyfin's own metadata carries both
a Tmdb and a Tvdb id for both of them), and a real pull-sync would silently
re-populate Show.tmdb_id from Jellyfin's ProviderIds regardless - but an
explicit restore keeps other tests_live runs deterministic.
"""
import httpx

from tests_live.helpers import trigger_and_wait_for_push, trigger_and_wait_for_sync

JOHN_DOE_SHOW_TMDB_ID = 689
JOHN_DOE_SEASON = 1
JOHN_DOE_EPISODE = 1


def test_tvdb_only_show_episode_is_pushed_to_jellyfin(
    scrob_api: httpx.Client, jellyfin_client: httpx.Client, jellyfin_conn: dict, dev_db
):
    trigger_and_wait_for_sync(scrob_api, "jellyfin")

    show_row = dev_db.fetchrow("SELECT id, tmdb_id FROM shows WHERE tmdb_id = $1", JOHN_DOE_SHOW_TMDB_ID)
    assert show_row is not None, "John Doe not found in Scrob's shows table - did the dev fixture change?"
    show_id = show_row["id"]
    original_tmdb_id = show_row["tmdb_id"]

    media_row = dev_db.fetchrow(
        "SELECT id FROM media WHERE show_id = $1 AND season_number = $2 AND episode_number = $3",
        show_id, JOHN_DOE_SEASON, JOHN_DOE_EPISODE,
    )
    assert media_row is not None, "John Doe S1E1 not found in Scrob's media table"
    media_id = media_row["id"]

    coll_file_row = dev_db.fetchrow(
        """
        SELECT cf.* FROM collection_files cf
        JOIN collections c ON c.id = cf.collection_id
        WHERE c.media_id = $1 AND cf.connection_id = $2
        """,
        media_id, int(jellyfin_conn["id"]),
    )
    assert coll_file_row is not None, "expected an existing Jellyfin collection_files row for John Doe S1E1 - did the sync above not run?"

    try:
        dev_db.execute("UPDATE shows SET tmdb_id = NULL WHERE id = $1", show_id)
        dev_db.execute("DELETE FROM collection_files WHERE id = $1", coll_file_row["id"])

        resp = scrob_api.post(
            "/history",
            json={
                "media_id": media_id,
                "media_type": "episode",
                "watched_at": "2024-01-01T12:00:00",
                "completed": True,
                "force": True,
            },
        )
        resp.raise_for_status()

        push_job = trigger_and_wait_for_push(scrob_api, int(jellyfin_conn["id"]))
        assert push_job["status"] == "completed", f"push job did not complete cleanly: {push_job}"
        assert push_job["stats"]["succeeded"] >= 1, (
            f"expected the TVDB fallback to resolve John Doe S1E1 with no cached source_id, got: {push_job}"
        )

        r = jellyfin_client.get(
            "/Items",
            params={"IncludeItemTypes": "Series", "Recursive": "true", "Fields": "ProviderIds"},
        )
        r.raise_for_status()
        series = next(i for i in r.json()["Items"] if i["Name"] == "John Doe")

        r = jellyfin_client.get(
            f"/Shows/{series['Id']}/Episodes",
            params={"season": JOHN_DOE_SEASON, "userId": jellyfin_conn["server_user_id"], "Fields": "UserData"},
        )
        r.raise_for_status()
        ep = next(i for i in r.json()["Items"] if i["IndexNumber"] == JOHN_DOE_EPISODE)
        assert ep["UserData"]["Played"] is True, "John Doe S1E1 was not actually marked watched on the real Jellyfin server"
    finally:
        dev_db.execute("UPDATE shows SET tmdb_id = $1 WHERE id = $2", original_tmdb_id, show_id)
        dev_db.execute(
            """
            INSERT INTO collection_files
                (id, collection_id, connection_id, source, source_id, resolution, video_codec,
                 audio_codec, audio_channels, audio_languages, subtitle_languages, file_path, added_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (id) DO NOTHING
            """,
            coll_file_row["id"], coll_file_row["collection_id"], coll_file_row["connection_id"],
            coll_file_row["source"], coll_file_row["source_id"], coll_file_row["resolution"],
            coll_file_row["video_codec"], coll_file_row["audio_codec"], coll_file_row["audio_channels"],
            coll_file_row["audio_languages"], coll_file_row["subtitle_languages"], coll_file_row["file_path"],
            coll_file_row["added_at"],
        )

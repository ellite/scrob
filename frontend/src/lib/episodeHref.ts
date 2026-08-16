// Builds the correct link for a season/episode/show card, respecting the
// user's per-show TVDB/TMDB numbering preference (#186). Previously every
// call site picked TMDB-vs-TVDB routing off `tvdb_sourced` (true only when
// an episode has NO TMDB counterpart at all - see is_unmapped_tvdb_episode
// in backend/core/enrichment.py) and raw id presence, neither of which has
// anything to do with what the user actually chose - a mapped episode of a
// "tvdb"-preference show would still route to the TMDB URL using numbers
// that can 404 if TMDB has renumbered since the show was matched.
//
// Preference (`show_episode_order`) beats raw field presence. When it's
// "tvdb" and a translated position is available (`tvdb_season_number`/
// `tvdb_episode_number`, attached by backend/routers/media.py's
// _attach_episode_order_fields), route TVDB with the translated numbers.
// Otherwise fall back to the pre-existing logic unchanged.
//
// Duplicated inline (not imported) in places that can't `import` inside an
// Astro `define:vars` client script - keep them in sync with this function
// if it changes: frontend/src/pages/next-up.astro (buildCard),
// frontend/src/pages/index.astro (episodeCardHtml),
// frontend/src/pages/list/[id].astro (buildCard, season/series branches
// only - this page has no episode-type cards), and
// frontend/src/layouts/Base.astro (renderNowPlaying).
export interface EpisodeHrefItem {
  type?: string | null;
  id?: number | string | null;
  tmdb_id?: number | null;
  tvdb_id?: number | null;
  show_tmdb_id?: number | null;
  show_tvdb_id?: number | null;
  season_number?: number | null;
  episode_number?: number | null;
  tvdb_sourced?: boolean;
  show_episode_order?: "tmdb" | "tvdb" | null;
  tvdb_season_number?: number | null;
  tvdb_episode_number?: number | null;
}

export function episodeHref(item: EpisodeHrefItem): string | null {
  const isEpisode = item.type === "episode";
  // A "series" item with season_number set is a season list item (see
  // routers/lists.py) - it shares media with the whole show.
  const isSeason = item.type === "series" && item.season_number != null;
  const isSeries =
    (item.type === "series" && item.season_number == null) ||
    (item.type === "episode" && !item.season_number && !item.id);
  const prefersTvdb = item.show_episode_order === "tvdb";

  if (isSeason) {
    if (
      prefersTvdb &&
      item.show_tvdb_id &&
      item.tvdb_season_number != null
    ) {
      return `/show/tvdb/${item.show_tvdb_id}/season/${item.tvdb_season_number}`;
    }
    if (item.show_tmdb_id || item.tmdb_id) {
      return `/show/${item.show_tmdb_id || item.tmdb_id}/season/${item.season_number}`;
    }
    if (item.show_tvdb_id) {
      return `/show/tvdb/${item.show_tvdb_id}/season/${item.season_number}`;
    }
  }

  if (isSeries) {
    if (prefersTvdb && item.tvdb_id) {
      return `/show/tvdb/${item.tvdb_id}`;
    }
    if (item.tvdb_id && !item.tmdb_id) return `/show/tvdb/${item.tvdb_id}`;
    if (item.tmdb_id) return `/show/${item.tmdb_id}`;
  }

  if (isEpisode) {
    // tvdb_sourced episodes have no TMDB counterpart at all, so their own
    // season_number/episode_number are ALREADY TVDB-native - never let the
    // translated-position branch below override them. Without this guard,
    // a coincidental numeric match between this episode's raw TVDB position
    // and some unrelated real episode's TMDB position in the same show's
    // mapping table (plausible - both are small integers) could route to
    // the wrong episode entirely.
    if (
      !item.tvdb_sourced &&
      prefersTvdb &&
      item.show_tvdb_id &&
      item.tvdb_season_number != null &&
      item.tvdb_episode_number != null
    ) {
      return `/show/tvdb/${item.show_tvdb_id}/season/${item.tvdb_season_number}/${item.tvdb_episode_number}`;
    }
    if (
      !item.tvdb_sourced &&
      item.show_tmdb_id &&
      item.season_number != null &&
      item.episode_number != null
    ) {
      return `/show/${item.show_tmdb_id}/season/${item.season_number}/${item.episode_number}`;
    }
    if (
      item.show_tvdb_id &&
      item.season_number != null &&
      item.episode_number != null
    ) {
      return `/show/tvdb/${item.show_tvdb_id}/season/${item.season_number}/${item.episode_number}`;
    }
    if (item.show_tmdb_id) return `/show/${item.show_tmdb_id}`;
    if (item.show_tvdb_id) return `/show/tvdb/${item.show_tvdb_id}`;
  }

  if (item.tmdb_id) return `/media/${item.type}/${item.tmdb_id}`;
  return null;
}

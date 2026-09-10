// Builds the correct link for a season/episode/show card.
//
// #174: a show can be on an alternate episode ordering (DVD, absolute, a TMDB
// episode group, ...). When it is, the backend attaches `show_episode_order`
// (a non-aired order key) plus the display position (`display_season_number` /
// `display_episode_number`, from _attach_episode_order_fields), and the link
// must use those numbers - the canonical season/episode numbers would land on
// the wrong episode under that ordering. Every show renders on `/show/{tmdb_id}`
// regardless of ordering; `/show/tvdb/{id}` is only for shows with no TMDB
// counterpart at all.
//
// Duplicated inline (not imported) in places that can't `import` inside an
// Astro `define:vars` client script - keep them in sync with this function
// if it changes: frontend/src/pages/next-up.astro (buildCard),
// frontend/src/pages/continue-watching.astro (buildCard, episode branch only),
// frontend/src/pages/index.astro (episodeCardHtml),
// frontend/src/pages/list/[id].astro (buildCard, season/series branches only),
// and frontend/src/layouts/Base.astro (renderNowPlaying).
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
  show_episode_order?: string | null;
  display_season_number?: number | null;
  display_episode_number?: number | null;
}

const ordered = (item: EpisodeHrefItem) =>
  !!item.show_episode_order && item.show_episode_order !== "tmdb:aired";

export function episodeHref(item: EpisodeHrefItem): string | null {
  const isEpisode = item.type === "episode";
  // A "series" item with season_number set is a season list item (see
  // routers/lists.py) - it shares media with the whole show.
  const isSeason = item.type === "series" && item.season_number != null;
  const isSeries =
    (item.type === "series" && item.season_number == null) ||
    (item.type === "episode" && !item.season_number && !item.id);

  if (isSeason) {
    if (item.show_tmdb_id || item.tmdb_id) {
      const showId = item.show_tmdb_id || item.tmdb_id;
      const s = ordered(item) && item.display_season_number != null
        ? item.display_season_number
        : item.season_number;
      return `/show/${showId}/season/${s}`;
    }
    if (item.show_tvdb_id) {
      return `/show/tvdb/${item.show_tvdb_id}/season/${item.season_number}`;
    }
  }

  if (isSeries) {
    if (item.tmdb_id) return `/show/${item.tmdb_id}`;
    if (item.tvdb_id) return `/show/tvdb/${item.tvdb_id}`;
  }

  if (isEpisode) {
    // tvdb_sourced episodes have no TMDB counterpart at all, so their own
    // season_number/episode_number are already the right numbers - the ordered
    // branch never applies to them.
    if (
      !item.tvdb_sourced &&
      ordered(item) &&
      item.show_tmdb_id &&
      item.display_season_number != null &&
      item.display_episode_number != null
    ) {
      return `/show/${item.show_tmdb_id}/season/${item.display_season_number}/${item.display_episode_number}`;
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

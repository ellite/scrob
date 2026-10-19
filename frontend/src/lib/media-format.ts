export function formatEpisodesLeft(
  episodesLeft?: number | null,
  remainingRuntime?: number | null,
): string | null {
  if (episodesLeft == null || episodesLeft <= 0) return null;
  const label = `${episodesLeft} episode${episodesLeft === 1 ? "" : "s"} left`;
  if (!remainingRuntime || remainingRuntime <= 0) return label;
  const h = Math.floor(remainingRuntime / 60);
  const m = remainingRuntime % 60;
  const runtime = h > 0 ? (m > 0 ? `~${h}h ${m}m` : `~${h}h`) : `~${m}m`;
  return `${label} · ${runtime}`;
}

export const WATCH_SOURCE_LABELS: Record<string, string> = {
  plex: "Plex",
  jellyfin: "Jellyfin",
  emby: "Emby",
  kodi: "Kodi",
  nuvio: "Nuvio",
  arvio: "Arvio",
  stremio: "Stremio",
  trakt: "Trakt",
  simkl: "Simkl",
  mdblist: "MDBList",
  wetrakr: "WeTrakr",
  bingebase: "Bingebase",
  yamtrack: "Yamtrack",
  manual: "Manual",
  scrob: "Scrob",
  import: "Import",
};

// Where a watch was recorded from (#477), or null for rows from before this
// was tracked.
export function watchSourceLabel(source?: string | null): string | null {
  if (!source) return null;
  return WATCH_SOURCE_LABELS[source] ?? source.charAt(0).toUpperCase() + source.slice(1);
}

export function formatWatchTime(minutes?: number | null): string | null {
  if (!minutes || minutes <= 0) return null;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return h > 0 ? (m > 0 ? `${h}h ${m}m` : `${h}h`) : `${m}m`;
}

export function formatSeasonTitle(seasonNumber: number, name?: string | null): string {
  const isSpecials = seasonNumber === 0;
  const fallback = isSpecials ? "Specials" : `Season ${seasonNumber}`;
  const trimDecorators = (value: string) => value.replace(/^[-–—:·\s]+|[-–—:·\s]+$/g, "").trim();
  // Strip a redundant leading label ("Season 3 - ", or "Specials - " for season 0)
  // so a name that only repeats the fallback collapses back to it.
  const prefixRe = isSpecials
    ? /^(?:season\s+0|specials)\s*[-–—:·]?\s*/i
    : new RegExp(`^season\\s+${seasonNumber}\\s*[-–—:·]?\\s*`, "i");
  const customName = trimDecorators(trimDecorators(name ?? "").replace(prefixRe, ""));
  return customName ? `${fallback} · ${customName}` : fallback;
}

// #174: the season/episode numbers to *show* for an episode-like item - the
// display position when the show is on a non-aired ordering, else the canonical
// ones. Pass the item straight from the API (episode card, history row, ...).
export function displaySeasonEpisode(item: {
  season_number?: number | null;
  episode_number?: number | null;
  show_episode_order?: string | null;
  display_season_number?: number | null;
  display_episode_number?: number | null;
}): { season: number | null; episode: number | null } {
  const ordered = !!item.show_episode_order && item.show_episode_order !== "tmdb:aired";
  return {
    season: ordered && item.display_season_number != null
      ? item.display_season_number
      : item.season_number ?? null,
    episode: ordered && item.display_episode_number != null
      ? item.display_episode_number
      : item.episode_number ?? null,
  };
}

export function episodeCode(item: Parameters<typeof displaySeasonEpisode>[0]): string | null {
  const { season, episode } = displaySeasonEpisode(item);
  if (season == null || episode == null) return null;
  return `S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
}

// "Watched Oct 12, 2026" for the most recent play (#317), or null when there
// isn't one with a known date.
export function formatWatchedOn(iso?: string | null, timeZone?: string): string | null {
  if (!iso) return null;
  const date = new Date(iso + "Z").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", timeZone });
  return `Watched ${date}`;
}

import { m } from "../paraglide/messages.js";
import { formatDate } from "./format";

// Compact runtime ("1h 5m", "45m") in the UI language.
export function formatShortDuration(totalMinutes: number): string {
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours === 0) return m.duration_minutes({ minutes });
  return minutes > 0 ? m.duration_hours_minutes({ hours, minutes }) : m.duration_hours({ hours });
}

export function formatEpisodesLeft(
  episodesLeft?: number | null,
  remainingRuntime?: number | null,
): string | null {
  if (episodesLeft == null || episodesLeft <= 0) return null;
  const label = m.episodes_left({ count: episodesLeft });
  if (!remainingRuntime || remainingRuntime <= 0) return label;
  return `${label} · ~${formatShortDuration(remainingRuntime)}`;
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
  scrob: "Scrob",
};

// Where a watch was recorded from (#477), or null for rows from before this
// was tracked. Service names are brands and stay as-is; "manual" and "import"
// are words, so they go through the UI language.
export function watchSourceLabel(source?: string | null): string | null {
  if (!source) return null;
  if (source === "manual") return m.watch_source_manual();
  if (source === "import") return m.watch_source_import();
  return WATCH_SOURCE_LABELS[source] ?? source.charAt(0).toUpperCase() + source.slice(1);
}

// Every known source, for client scripts that label rows they render themselves.
export const WATCH_SOURCES = [...Object.keys(WATCH_SOURCE_LABELS), "manual", "import"];

export function formatWatchTime(minutes?: number | null): string | null {
  if (!minutes || minutes <= 0) return null;
  return formatShortDuration(Math.round(minutes));
}

export function formatSeasonTitle(seasonNumber: number, name?: string | null): string {
  const isSpecials = seasonNumber === 0;
  const fallback = isSpecials ? m.season_specials() : m.media_season_number({ number: seasonNumber });
  const trimDecorators = (value: string) => value.replace(/^[-–—:·\s]+|[-–—:·\s]+$/g, "").trim();
  // Strip a redundant leading label ("Season 3 - ", or "Specials - " for season 0)
  // so a name that only repeats the fallback collapses back to it. Metadata may
  // be in English or in the metadata language, and the fallback is in the UI
  // language, so both the English form and the localized fallback are matched.
  const escapeRe = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
  const prefixRe = isSpecials
    ? new RegExp(`^(?:season\\s+0|specials|${escapeRe(fallback)})\\s*[-–—:·]?\\s*`, "i")
    : new RegExp(`^(?:season\\s+${seasonNumber}|${escapeRe(fallback)})\\s*[-–—:·]?\\s*`, "i");
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
  const date = formatDate(iso + "Z", { month: "short", day: "numeric", year: "numeric", timeZone });
  return m.media_watched_on({ date });
}

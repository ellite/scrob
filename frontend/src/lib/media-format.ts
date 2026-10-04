import { m } from "../paraglide/messages.js";

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

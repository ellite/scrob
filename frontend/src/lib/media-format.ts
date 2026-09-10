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

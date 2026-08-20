/**
 * @typedef {{ episode_number: number, tmdb_rating?: number | null, title?: string | null, name?: string | null }} EpisodeRating
 * @typedef {{ episodeNumber: number, title: string | null, rating: number | null }} SeasonRatingEntry
 */

/**
 * Turn the episode data already supplied by a season endpoint into values that
 * can be displayed on a 0–10 chart. Zero and invalid values mean unavailable.
 *
 * @param {EpisodeRating[]} episodes
 * @returns {SeasonRatingEntry[]}
 */
export function buildSeasonRatingEntries(episodes) {
  return episodes
    .map((episode) => {
      const value = Number(episode.tmdb_rating);
      const rating = Number.isFinite(value) && value > 0 ? Math.min(value, 10) : null;
      return {
        episodeNumber: episode.episode_number,
        title: episode.title ?? episode.name ?? null,
        rating,
      };
    })
    .filter((episode) => Number.isFinite(episode.episodeNumber))
    .sort((a, b) => a.episodeNumber - b.episodeNumber);
}

/**
 * @param {SeasonRatingEntry[]} entries
 */
export function summarizeEpisodeRatings(entries) {
  const ratedEntries = entries.filter((episode) => episode.rating !== null);
  const highRating = ratedEntries.length ? Math.max(...ratedEntries.map((episode) => episode.rating)) : null;
  const lowRating = ratedEntries.length ? Math.min(...ratedEntries.map((episode) => episode.rating)) : null;

  return {
    ratedEntries,
    highRating,
    lowRating,
    hasRange: highRating !== null && lowRating !== null && highRating !== lowRating,
    highest: ratedEntries.find((episode) => episode.rating === highRating),
    lowest: ratedEntries.find((episode) => episode.rating === lowRating),
  };
}

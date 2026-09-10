export interface PosterMedia {
  type?: string;
  media_type?: string;
  tmdb_id?: number | null;
  tvdb_id?: number | null;
  imdb_id?: string | null;
  show_tmdb_id?: number | null;
  show_tvdb_id?: number | null;
  season_number?: number | null;
}

// Keep the inline counterpart in Base.astro in sync: define:vars scripts cannot
// import this module. Only call for portrait slots, never episode stills.
export function ratingPosterUrl(
  fallback: string | null | undefined,
  item: PosterMedia,
  apiKey?: string | null,
): string | null {
  if (!apiKey) return fallback ?? null;
  const type = item.type ?? item.media_type;
  const isEpisode = type === "episode";
  if (type !== "movie" && type !== "series" && !isEpisode) return fallback ?? null;
  if (type === "series" && item.season_number != null) return fallback ?? null;

  const tmdbId = isEpisode ? item.show_tmdb_id : item.tmdb_id;
  const tvdbId = isEpisode ? item.show_tvdb_id : item.tvdb_id;
  const mediaType = type === "movie" ? "movie" : "series";
  let provider: string;
  let id: string;
  if (Number.isInteger(tmdbId) && tmdbId! > 0) {
    provider = "tmdb";
    id = `${mediaType}-${tmdbId}`;
  } else if (Number.isInteger(tvdbId) && tvdbId! > 0) {
    provider = "tvdb";
    id = `${mediaType}-${tvdbId}`;
  } else if (!isEpisode && /^tt\d+$/.test(item.imdb_id ?? "")) {
    provider = "imdb";
    id = item.imdb_id!;
  } else {
    return fallback ?? null;
  }

  // The fragment stays in the browser, so the original artwork is available
  // on failure without leaking that URL to RPDB or adding a proxy request.
  const recovery = fallback ? `#scrob-fallback=${encodeURIComponent(fallback)}` : "";
  return `https://api.ratingposterdb.com/${encodeURIComponent(apiKey)}/${provider}/poster-default/${id}.jpg?fallback=true${recovery}`;
}

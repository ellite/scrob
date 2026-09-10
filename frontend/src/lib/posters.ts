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

// #377: for a movie/show portrait poster, route through the backend
// rating-poster proxy so the RPDB image (rating overlay) is served with the
// user's key kept server-side - exactly like `/api/proxy/media/image/` for
// TMDB. `enabled` is a plain boolean (the viewer has an RPDB key); when false
// or the slot isn't eligible, the caller's already-proxied poster is returned
// unchanged. Never call for episode stills or season artwork.
//
// Keep the inline counterpart in Base.astro's <head> script in sync -
// define:vars scripts cannot import this module.
export function ratingPosterUrl(
  fallback: string | null | undefined,
  item: PosterMedia,
  enabled?: boolean,
): string | null {
  const fb = fallback ?? null;
  if (!enabled || !fb || !fb.startsWith("/api/proxy/media/image/")) return fb;

  const type = item.type ?? item.media_type;
  const isEpisode = type === "episode";
  if (type !== "movie" && type !== "series" && !isEpisode) return fb;
  if (type === "series" && item.season_number != null) return fb;

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
    return fb;
  }

  return `/api/proxy/media/rating-poster/${provider}/${id}?fallback=${encodeURIComponent(fb)}`;
}

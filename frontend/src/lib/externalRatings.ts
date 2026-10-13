// Selectable rating source (Settings -> Display -> Rating source).
//
// "tmdb" keeps everything exactly as before. Any other source comes from
// MDBList through the backend (/external-ratings), which caches one row per
// title in the external_ratings table. Pages render the badges server-side
// from a per-request map of cached values, and /external-ratings-client.js
// fills in titles that aren't cached yet (and badges drawn by client-side
// scripts) as they scroll into view.
import { api } from "./api";

export type RatingSource = "tmdb" | "imdb" | "tomatometer" | "popcornmeter" | "metacritic" | "letterboxd" | "trakt";
export type ExtSource = Exclude<RatingSource, "tmdb">;
type Fmt = "pct" | "dec1" | "int";

export const SOURCE_META: Record<ExtSource, { name: string; icon?: string; short?: string; fmt: Fmt }> = {
  imdb:         { name: "IMDb",                          short: "IMDb",  fmt: "dec1" },
  tomatometer:  { name: "Rotten Tomatoes Tomatometer",   icon: "🍅",     fmt: "pct" },
  popcornmeter: { name: "Rotten Tomatoes Popcornmeter",  icon: "🍿",     fmt: "pct" },
  metacritic:   { name: "Metacritic",                    short: "MC",    fmt: "int" },
  letterboxd:   { name: "Letterboxd",                    short: "LB",    fmt: "dec1" },
  trakt:        { name: "Trakt",                         short: "Trakt", fmt: "pct" },
};

export const SOURCE_OPTIONS: { value: RatingSource; label: string }[] = [
  { value: "tmdb", label: "TMDB (default)" },
  { value: "imdb", label: "IMDb" },
  { value: "tomatometer", label: "Rotten Tomatoes – Tomatometer (critics)" },
  { value: "popcornmeter", label: "Rotten Tomatoes – Popcornmeter (audience)" },
  { value: "metacritic", label: "Metacritic" },
  { value: "letterboxd", label: "Letterboxd" },
  { value: "trakt", label: "Trakt" },
];

export function formatValue(source: ExtSource, value: number): string {
  const fmt = SOURCE_META[source].fmt;
  if (fmt === "pct") return `${Math.round(value)}%`;
  if (fmt === "int") return String(Math.round(value));
  return value.toFixed(1);
}

/** The icon or short label in front of the value. */
export function prefixHtml(source: ExtSource): string {
  const m = SOURCE_META[source];
  return m.icon
    ? `<span class="leading-none">${m.icon}</span>`
    : `<span class="text-[0.8em] font-black tracking-tight opacity-80">${m.short}</span>`;
}

export const TMDB_STAR_HTML = (rating: string) =>
  `<span class="text-yellow-400 inline-flex items-center gap-1"><span class="leading-none">★</span>${rating}</span>`;
export const SEPARATOR_HTML = `<span class="ext-sep w-px h-3 bg-zinc-600 mx-0.5"></span>`;

/** Inner HTML of the external part of a badge: value, "–" when MDBList has none, "…" while loading. */
export function extPartHtml(source: ExtSource, value: number | null, pending = false): string {
  const meta = SOURCE_META[source];
  const text = pending ? "…" : value == null ? "–" : formatValue(source, value);
  const cls = pending ? "text-zinc-500 animate-pulse" : "text-zinc-100";
  return `<span class="ext-rt inline-flex items-center gap-1 ${cls}" title="${meta.name}">${prefixHtml(source)}${text}</span>`;
}

export interface RatingContext {
  source: ExtSource;
  showTmdb: boolean;
  values: Record<string, number>;
  stale: string[];
  missing: Set<string>;
}

// One /external-ratings/map call per user per few seconds, shared by every
// poster on the page (and by quick back-to-back navigations).
const MAP_TTL_MS = 10_000;
const mapCache = new Map<string, { at: number; ctx: Promise<RatingContext | null> }>();

/** Null when the user shows TMDB (the default) - callers then render exactly as before. */
export function ratingContext(locals: App.Locals): Promise<RatingContext | null> {
  const source = (locals.settings as any)?.rating_source as RatingSource | undefined;
  const token = locals.token;
  if (!token || !source || source === "tmdb") return Promise.resolve(null);
  const hit = mapCache.get(token);
  if (hit && Date.now() - hit.at < MAP_TTL_MS) return hit.ctx;
  const ctx = api.externalRatings.map(token)
    .then((m) => (m.source === "tmdb" ? null : {
      source: m.source as ExtSource, showTmdb: m.show_tmdb, values: m.values, stale: m.stale, missing: new Set(m.missing),
    }))
    .catch(() => null);
  mapCache.set(token, { at: Date.now(), ctx });
  if (mapCache.size > 200) mapCache.delete(mapCache.keys().next().value!);
  return ctx;
}

/** "m123" / "s45" - the key format of the map. */
export function shortKey(kind: "movie" | "show", id: number | string): string {
  return (kind === "movie" ? "m" : "s") + id;
}

/** Which title a poster's rating belongs to: shows, seasons and episodes use the show. */
export function cardTarget(item: { type?: string; tmdb_id?: number | null; show_tmdb_id?: number | null }):
  { kind: "movie" | "show"; id: number } | null {
  if (item.type === "movie" && item.tmdb_id) return { kind: "movie", id: item.tmdb_id };
  if (item.type === "series" || item.type === "episode") {
    const id = item.show_tmdb_id || (item.type === "series" ? item.tmdb_id : null);
    return id ? { kind: "show", id } : null;
  }
  return null;
}

/** Inner HTML for a poster badge, or null to keep the original TMDB badge. */
export function cardBadgeHtml(ctx: RatingContext, tmdbRating: string | null, key: string | null): string | null {
  if (!key) return null;
  const value = ctx.values[key];
  if (value !== undefined) {
    const ext = extPartHtml(ctx.source, value);
    return ctx.showTmdb && tmdbRating ? TMDB_STAR_HTML(tmdbRating) + SEPARATOR_HTML + ext : ext;
  }
  if (ctx.missing.has(key)) {
    // MDBList has no value for this source: TMDB alone, or "src –" when there's no TMDB rating either.
    if (!tmdbRating) return extPartHtml(ctx.source, null);
    return ctx.showTmdb ? TMDB_STAR_HTML(tmdbRating) + SEPARATOR_HTML + extPartHtml(ctx.source, null) : TMDB_STAR_HTML(tmdbRating);
  }
  // Not cached yet: a placeholder that /external-ratings-client.js fills in when the poster is visible.
  const pending = extPartHtml(ctx.source, null, true);
  return ctx.showTmdb && tmdbRating ? TMDB_STAR_HTML(tmdbRating) + SEPARATOR_HTML + pending : pending;
}

const BACKEND_PORT = (import.meta.env.BACKEND_PORT as string | undefined) ?? "7331";
const BASE = `http://localhost:${BACKEND_PORT}`;

async function request<T>(
  path: string,
  method: string = "GET",
  params?: Record<string, string | number | boolean | undefined>,
  body?: unknown,
  token?: string
): Promise<T> {
  const url = new URL(path.startsWith("/") ? path.slice(1) : path, BASE.endsWith("/") ? BASE : BASE + "/");
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined) url.searchParams.set(k, String(v));
    });
  }

  const headers: Record<string, string> = {};
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  let finalBody: BodyInit | undefined;
  if (body instanceof FormData) {
    finalBody = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    finalBody = JSON.stringify(body);
  }

  const res = await fetch(url.toString(), {
    method,
    headers,
    body: finalBody,
  });

  if (!res.ok) {
    let errorDetail = "";
    try {
      const errorJson = await res.json();
      errorDetail = errorJson.detail || JSON.stringify(errorJson);
    } catch (e) {}
    throw new Error(`API ${res.status}: ${path} ${errorDetail}`);
  }
  return res.json();
}

async function get<T>(path: string, params?: Record<string, string | number | boolean | undefined>, token?: string): Promise<T> {
  return request<T>(path, "GET", params, undefined, token);
}

async function post<T>(path: string, body?: unknown, token?: string): Promise<T> {
  return request<T>(path, "POST", undefined, body, token);
}

async function put<T>(path: string, body?: unknown, token?: string): Promise<T> {
  return request<T>(path, "PUT", undefined, body, token);
}

async function patch<T>(path: string, body?: unknown, token?: string): Promise<T> {
  return request<T>(path, "PATCH", undefined, body, token);
}

async function del<T>(path: string, token?: string): Promise<T> {
  return request<T>(path, "DELETE", undefined, undefined, token);
}

// Shared sub-types

export interface CastMember {
  tmdb_id: number;
  name: string;
  character: string;
  profile_path: string | null;
}

export interface Network {
  id: number;
  name: string;
  logo_path: string | null;
}

export interface ProductionCompany {
  id: number;
  name: string;
  logo_path: string | null;
}

export interface SeasonMeta {
  season_number: number;
  name: string;
  overview: string | null;
  poster_path: string | null;
  episode_count: number;
  air_date: string | null;
  tmdb_season_id?: number | null;
  tmdb_rating?: number | null;
}

export interface SeasonState {
  watched: boolean;
  in_library: boolean;
  collection_pct: number;
  user_rating: number | null;
}

export interface EpisodeItem {
  id: number | null;
  tmdb_id: number;
  episode_number: number;
  title: string;
  overview: string | null;
  air_date: string | null;
  poster_path: string | null;
  tmdb_rating: number;
  runtime: number | null;
  in_library: boolean;
  watched: boolean;
  user_rating: number | null;
  in_lists: number[];
}

export interface ShowSummary {
  tmdb_id: number;
  title: string;
  poster_path: string | null;
  backdrop_path: string | null;
  seasons_meta: SeasonMeta[];
}

export interface Season {
  show: ShowSummary;
  name: string;
  overview: string | null;
  poster_path: string | null;
  backdrop_path: string | null;
  air_date: string | null;
  tmdb_rating?: number | null;
  episodes: EpisodeItem[];
  season_number: number;
  show_watched: boolean;
  season_watched: boolean;
  season_in_library: boolean;
  season_collection_pct: number;
  season_user_rating: number | null;
  show_in_lists: number[];
  show_in_library: boolean;
  show_collection_pct: number;
  show_request_enabled: boolean;
  show_is_monitored: boolean;
}

export interface EpisodeDetail {
  show: ShowSummary;
  title: string;
  overview: string | null;
  still_path: string | null;
  air_date: string | null;
  episode_number: number;
  season_number: number;
  runtime: number | null;
  tmdb_rating: number | null;
  tmdb_id: number;
  id: number | null;
  in_library: boolean;
  watched: boolean;
  in_lists: number[];
  collection_pct: number;
  user_rating?: number | null;
  cast: CastMember[];
  guest_stars: CastMember[];
  episodes: EpisodeItem[];
  library: {
    resolution: string;
    video_codec: string;
    audio_codec: string;
    audio_channels: string;
    audio_languages: string[];
    subtitle_languages: string[];
  } | null;
}

export interface PersonCredit {
  tmdb_id: number;
  type: "movie" | "series";
  title: string;
  poster_path: string | null;
  release_date: string | null;
  character: string | null;
  watched?: boolean;
  in_lists?: number[];
  in_library?: boolean;
  collection_pct?: number;
}

export interface PersonDetail {
  tmdb_id: number;
  name: string;
  profile_path: string | null;
  known_for_department: string | null;
  biography: string | null;
  birthday: string | null;
  place_of_birth: string | null;
  credits: PersonCredit[];
  in_lists: number[];
}

export interface WatchEvent {
  id: number;
  media: MediaItem;
  user_id: number;
  watched_at: string;
  completed: boolean;
  progress_percent: number | null;
}

export interface SyncJob {
  id: number;
  source: string;
  status: string;
  total_items: number;
  processed_items: number;
  error_message: string | null;
  updated_at: string;
}

export interface UserList {
  id: number;
  name: string;
  description: string | null;
  privacy_level: PrivacyLevel;
  item_count: number;
  created_at: string;
  updated_at: string;
  preview_posters: string[];
}

export interface PublicList extends UserList {
  username: string;
}

export interface ListItemEntry {
  id: number;
  list_id: number;
  added_at: string;
  sort_order: number;
  notes: string | null;
  media: MediaItem;
}

export interface ListDetail extends UserList {
  items: ListItemEntry[];
  is_owner: boolean;
}

// Main types

export type MediaType = "movie" | "series" | "episode" | "person" | "collection";

export interface UserProfile {
  id: number;
  email: string;
  username: string;
  display_name: string;
  role: string;
  api_key: string;
  totp_enabled: boolean;
  created_at: string;
}

export interface LoginResponse {
  access_token: string | null;
  token_type: string;
  requires_2fa: boolean;
  temp_token: string | null;
}

export interface TotpSetupData {
  provisioning_uri: string;
  secret: string;
}

export interface TotpBackupCode {
  id: number;
  code: string;
  used: boolean;
}

export interface TotpBackupCodesResponse {
  codes: TotpBackupCode[];
}

export interface OidcConfig {
  enabled: boolean;
  provider_name: string;
  disable_password_login: boolean;
}

export interface OidcAuthorizeResponse {
  auth_url: string;
  state: string;
}

export interface OidcExchangeResponse {
  access_token: string;
}

export type PrivacyLevel = "public" | "friends_only" | "private";

export interface UserPreferences {
  display_name: string | null;
  bio: string | null;
  country: string | null;
  movie_genres: string[];
  show_genres: string[];
  streaming_services: string[];
  content_language: string | null;
  privacy_level: PrivacyLevel;
  avatar_url: string | null;
}

export interface UserSettings {
  tmdb_api_key: string | null;
  jellyfin_url: string | null;
  jellyfin_token: string | null;
  jellyfin_user_id: string | null;
  emby_url: string | null;
  emby_token: string | null;
  emby_user_id: string | null;
  plex_url: string | null;
  plex_token: string | null;
  
  radarr_url: string | null;
  radarr_token: string | null;
  radarr_root_folder: string | null;
  radarr_quality_profile: number | null;
  radarr_tags: number[] | null;

  sonarr_url: string | null;
  sonarr_token: string | null;
  sonarr_root_folder: string | null;
  sonarr_quality_profile: number | null;
  sonarr_tags: number[] | null;

  // Inbound sync flags (source → Abstract)
  plex_sync_collection: boolean;
  plex_sync_watched: boolean;
  plex_sync_ratings: boolean;
  plex_sync_playback: boolean;
  jellyfin_sync_collection: boolean;
  jellyfin_sync_watched: boolean;
  jellyfin_sync_ratings: boolean;
  jellyfin_sync_playback: boolean;
  emby_sync_collection: boolean;
  emby_sync_watched: boolean;
  emby_sync_ratings: boolean;
  emby_sync_playback: boolean;

  // Outbound push flags (Abstract → source)
  plex_push_watched: boolean;
  plex_push_ratings: boolean;
  jellyfin_push_watched: boolean;
  jellyfin_push_ratings: boolean;
  emby_push_watched: boolean;
  emby_push_ratings: boolean;

  // Trakt
  trakt_connected: boolean;
  trakt_sync_watched: boolean;
  trakt_sync_ratings: boolean;
  trakt_push_watched: boolean;
  trakt_push_ratings: boolean;

  jellyfin_auto_sync_interval: number | null;
  emby_auto_sync_interval: number | null;
  plex_auto_sync_interval: number | null;

  preferences: UserPreferences | null;
}

export interface ServiceStatus {
  configured: boolean;
  connected: boolean;
  quality_profiles?: { id: number; name: string }[];
  root_folders?: { path: string; freeSpace: number }[];
  tags?: { id: number; label: string }[];
}

export interface ConnectionStatus {
  jellyfin: ServiceStatus;
  emby: ServiceStatus;
  plex: ServiceStatus;
  radarr: ServiceStatus;
  sonarr: ServiceStatus;
  trakt: ServiceStatus;
}

export interface MediaItem {
  id: number | null;
  tmdb_id: number;
  type: MediaType;
  title: string;
  original_title?: string | null;
  overview?: string | null;
  poster_path: string | null;
  backdrop_path?: string | null;
  release_date?: string | null;
  tmdb_rating?: number | null;
  season_number?: number | null;
  episode_number?: number | null;
  runtime?: number | null;
  genres?: string[];
  cast?: CastMember[];
  tagline?: string | null;
  status?: string | null;
  original_language?: string | null;
  age_rating?: string | null;
  show_title?: string | null;
  show_tmdb_id?: number | null;
  show_poster_path?: string | null;
  show_backdrop_path?: string | null;
  known_for_department?: string | null;
  in_library?: boolean;
  // Card action state
  watched?: boolean;
  in_lists?: number[];
  collection_pct?: number;
  is_monitored?: boolean;
  request_enabled?: boolean;
  user_rating?: number | null;
  library: {
    resolution: string;
    video_codec: string;
    audio_codec: string;
    audio_channels: string;
    audio_languages: string[];
    subtitle_languages: string[];
  } | null;
  collection?: {
    id: number;
    name: string;
    poster_path: string | null;
    backdrop_path: string | null;
    parts: MediaItem[];
  };
  production_companies?: ProductionCompany[];
  recommendations?: MediaItem[];
}

export interface NowPlayingMedia {
  id: number;
  tmdb_id: number;
  type: MediaType;
  title: string;
  poster_path: string | null;
  backdrop_path: string | null;
  season_number: number | null;
  episode_number: number | null;
  runtime: number | null;
  show_title?: string;
  show_tmdb_id?: number;
  show_poster_path?: string | null;
}

export interface NowPlayingSession {
  session_key: string;
  source: string;
  state: "playing" | "paused";
  progress_percent: number;
  progress_seconds: number;
  started_at: string;
  updated_at: string;
  media: NowPlayingMedia;
}

export interface ContinueWatchingItem {
  id: number;
  media: MediaItem;
  user_id: number;
  watched_at: string;
  progress_seconds: number | null;
  progress_percent: number | null;
  completed: boolean;
}

export interface CollectionDetail {
  id: number;
  name: string;
  overview: string | null;
  poster_path: string | null;
  backdrop_path: string | null;
  genres: string[];
  cast: { tmdb_id: number; name: string; profile_path: string | null; appearances: number }[];
  parts: MediaItem[];
}

export interface Show {
  id: number | null;
  tmdb_id: number;
  title: string;
  original_title: string | null;
  overview: string;
  poster_path: string | null;
  backdrop_path: string | null;
  tmdb_rating: number;
  genres: string[];
  in_library: boolean;
  watched: boolean;
  in_lists: number[];
  collection_pct: number;
  is_monitored?: boolean;
  request_enabled?: boolean;
  seasons: Record<string, MediaItem[]>;
  seasons_meta: SeasonMeta[];
  season_states: Record<number, SeasonState>;
  cast: CastMember[];
  networks: Network[];
  recommendations: MediaItem[];
  tagline: string | null;
  status: string | null;
  original_language?: string | null;
  age_rating?: string | null;
  user_rating?: number | null;
  first_air_date: string | null;
  last_air_date: string | null;
}

export interface ProfileWatchedItem {
  tmdb_id: number;
  media_type: string;
  title: string;
  poster_path: string | null;
  backdrop_path: string | null;
  watched_at: string;
  show_title: string | null;
  show_tmdb_id: number | null;
  show_poster_path: string | null;
  season_number: number | null;
  episode_number: number | null;
}

export interface ProfileRatedItem {
  tmdb_id: number;
  media_type: string;
  title: string;
  poster_path: string | null;
  backdrop_path: string | null;
  user_rating: number;
}

export interface UserSearchResult {
  id: number;
  username: string;
  display_name: string;
  avatar_url: string | null;
  country: string | null;
  movies_watched: number;
  shows_watched: number;
  total_collected: number;
  total_rated: number;
  follower_count: number;
  is_following: boolean;
  is_self: boolean;
}

export interface ProfileFollowEntry {
  id: number;
  display_name: string;
  avatar_url: string | null;
}

export interface ProfileListItem {
  id: number;
  name: string;
  description: string | null;
  privacy_level: PrivacyLevel;
  item_count: number;
  updated_at: string;
  preview_posters: string[];
}

export interface ProfileCommentItem {
  id: number;
  content: string;
  media_type: string;
  tmdb_id: number;
  season_number: number | null;
  episode_number: number | null;
  title: string | null;
  poster_path: string | null;
  created_at: string;
}

export interface PublicProfile {
  id: number;
  username: string;
  display_name: string;
  bio: string | null;
  country: string | null;
  movie_genres: string[];
  show_genres: string[];
  created_at: string;
  total_watched: number;
  total_collected: number;
  movies_watched: number;
  shows_watched: number;
  total_rated: number;
  avatar_url: string | null;
  recently_watched_movies: ProfileWatchedItem[];
  recently_watched_shows: ProfileWatchedItem[];
  top_rated_movies: ProfileRatedItem[];
  top_rated_shows: ProfileRatedItem[];
  recent_comments: ProfileCommentItem[];
  lists: ProfileListItem[];
  follower_count: number;
  following_count: number;
  followers: ProfileFollowEntry[];
  following: ProfileFollowEntry[];
  is_following: boolean;
}

export interface UserStats {
  movies_watched: number;
  shows_watched: number;
  episodes_watched: number;
  total_watch_minutes: number;
  watch_activity: { month: string; movies: number; episodes: number }[];
  rating_distribution: { rating: number; count: number }[];
  avg_movie_rating: number | null;
  avg_show_rating: number | null;
  movies_collected: number;
  shows_collected: number;
  episodes_collected: number;
  movies_watched_collected: number;
  movies_unwatched_collected: number;
  shows_watched_collected: number;
  shows_unwatched_collected: number;
  weekday_activity: { day: string; avg: number }[];
}

export interface Comment {
  id: number;
  user_id: number;
  username: string;
  display_name: string;
  avatar_url: string | null;
  user_is_public: boolean;
  content: string;
  created_at: string;
  updated_at?: string | null;
}

// API calls
export const api = {
  auth: {
    login: (body: FormData) =>
      post<LoginResponse>("/auth/login", body),
    register: (body: unknown) =>
      post<{ id: number; email: string; username: string }>("/auth/register", body),
    registrationStatus: () =>
      get<{ enabled: boolean; smtp_configured: boolean }>("/auth/registration-status"),
    activateEmail: (token: string) =>
      post<{ success: boolean }>(`/auth/activate/${token}`, undefined),
    forgotPassword: (email: string) =>
      post<{ message: string }>("/auth/forgot-password", { email }),
    resetPassword: (token: string, new_password: string) =>
      post<{ message: string }>(`/auth/reset-password/${token}`, { new_password }),
    me: (token: string) =>
      get<UserProfile>("/auth/me", undefined, token),
    getSettings: (token: string) =>
      get<UserSettings>("/auth/settings", undefined, token),
    updateSettings: (settings: Partial<UserSettings>, token: string) =>
      patch<UserSettings>("/auth/settings", settings, token),
    changePassword: (body: unknown, token: string) =>
      post<{ message: string }>("/auth/change-password", body, token),
    deleteAccount: (token: string) =>
      del<{ message: string }>("/auth/me", token),
    regenerateApiKey: (token: string) =>
      post<UserProfile>("/auth/api-key/regenerate", undefined, token),
    testJellyfin: (url: string, token: string, jellyfinUserId: string | null, userToken: string) =>
      post<{ success: boolean; message: string }>(`/auth/test-jellyfin?url=${encodeURIComponent(url)}&token=${encodeURIComponent(token)}${jellyfinUserId ? `&user_id=${encodeURIComponent(jellyfinUserId)}` : ""}`, undefined, userToken),
    testEmby: (url: string, token: string, embyUserId: string | null, userToken: string) =>
      post<{ success: boolean; message: string }>(`/auth/test-emby?url=${encodeURIComponent(url)}&token=${encodeURIComponent(token)}${embyUserId ? `&user_id=${encodeURIComponent(embyUserId)}` : ""}`, undefined, userToken),
    testPlex: (url: string, token: string, userToken: string) =>
      post<{ success: boolean; message: string }>(`/auth/test-plex?url=${encodeURIComponent(url)}&token=${encodeURIComponent(token)}`, undefined, userToken),
    testRadarr: (url: string, token: string, userToken: string) =>
      post<{ success: boolean; message: string }>(`/auth/test-radarr?url=${encodeURIComponent(url)}&token=${encodeURIComponent(token)}`, undefined, userToken),
    getRadarrProfiles: (url: string, token: string, userToken: string) =>
      get<{ quality_profiles: any[]; root_folders: any[] }>(`/auth/radarr/profiles?url=${encodeURIComponent(url)}&token=${encodeURIComponent(token)}`, undefined, userToken),
    testSonarr: (url: string, token: string, userToken: string) =>
      post<{ success: boolean; message: string }>(`/auth/test-sonarr?url=${encodeURIComponent(url)}&token=${encodeURIComponent(token)}`, undefined, userToken),
    getSonarrProfiles: (url: string, token: string, userToken: string) =>
      get<{ quality_profiles: any[]; root_folders: any[]; language_profiles: any[] }>(`/auth/sonarr/profiles?url=${encodeURIComponent(url)}&token=${encodeURIComponent(token)}`, undefined, userToken),
    testTmdb: (key: string, userToken: string) =>
      post<{ success: boolean; message: string }>(`/auth/test-tmdb?key=${encodeURIComponent(key)}`, undefined, userToken),
    getConnectionStatus: (token: string) =>
      get<ConnectionStatus>("/auth/connection-status", undefined, token),
    totp2faSetup: (token: string) =>
      post<TotpSetupData>("/auth/2fa/setup", undefined, token),
    totp2faEnable: (body: { secret: string; code: string }, token: string) =>
      post<TotpBackupCodesResponse>("/auth/2fa/enable", body, token),
    totp2faDisable: (body: { code: string }, token: string) =>
      post<{ status: string }>("/auth/2fa/disable", body, token),
    totp2faBackupCodes: (token: string) =>
      get<TotpBackupCodesResponse>("/auth/2fa/backup-codes", undefined, token),
    totp2faVerifyLogin: (body: { temp_token: string; code: string }) =>
      post<LoginResponse>("/auth/2fa/verify-login", body),
    oidcConfig: () =>
      get<OidcConfig>("/auth/oidc/config"),
    oidcAuthorize: () =>
      get<OidcAuthorizeResponse>("/auth/oidc/authorize"),
    oidcExchange: (code: string) =>
      post<OidcExchangeResponse>("/auth/oidc/exchange", { code }),
  },

  trakt: {
    deviceStart: (token: string) =>
      post<{ user_code: string; verification_url: string; expires_in: number; interval: number }>("/trakt/auth/device/start", undefined, token),
    devicePoll: (token: string) =>
      post<{ status: "pending" | "connected" }>("/trakt/auth/device/poll", undefined, token),
    disconnect: (token: string) =>
      del<{ status: string }>("/trakt/auth/disconnect", token),
    sync: (token: string) =>
      post<{ status: string; job_id: number; message: string }>("/trakt/sync", undefined, token),
  },

  media: {
    list: (params?: { type?: string; sort?: string; page?: number; genre?: string; year?: number }, token?: string) =>
      get<{ page: number; page_size: number; total_pages: number; total_results: number; results: MediaItem[] }>("/media", params, token),

    get: (type: string, tmdbId: number, token?: string) =>
      get<MediaItem>(`/media/${type}/${tmdbId}`, undefined, token),

    getRecommendations: (type: string, tmdbId: number, token?: string) =>
      get<{ results: MediaItem[] }>(`/media/${type}/${tmdbId}/recommendations`, undefined, token),

    getPerson: (personId: number, token?: string) =>
      get<PersonDetail>(`/media/person/${personId}`, undefined, token),

    getCollection: (collectionId: number, token?: string) =>
      get<CollectionDetail>(`/media/collection/${collectionId}`, undefined, token),

    tmdbList: (params: { type: string; category?: string; page?: number; genre?: string; year?: number; min_rating?: number; status?: string }, token?: string) =>
      get<{ results: MediaItem[]; page: number; total_pages: number; total_results: number }>("/media/tmdb/list", params, token),

    search: (q: string, type?: string, page: number = 1, year?: number, token?: string) =>
      get<{ results: MediaItem[]; page: number; total_pages: number; total_results: number }>("/media/search", { q, ...(type ? { type } : {}), page, ...(year ? { year } : {}) }, token),

    recentlyAdded: (type?: string, token?: string) =>
      get<{ results: MediaItem[] }>("/media/recently-added", type ? { type } : {}, token),

    onAirToday: (page: number = 1, token?: string) =>
      get<{ results: MediaItem[]; page: number; total_pages: number; total_results: number }>("/media/on-air-today", { page }, token),

    airingTodayCollected: (token?: string) =>
      get<{ results: MediaItem[] }>("/media/airing-today/collected", {}, token),

    trendingMovies: (page: number = 1, token?: string) =>
      get<{ results: MediaItem[]; page: number; total_pages: number; total_results: number }>("/media/trending/movies", { page }, token),

    trendingShows: (page: number = 1, token?: string) =>
      get<{ results: MediaItem[]; page: number; total_pages: number; total_results: number }>("/media/trending/shows", { page }, token),

    nowPlaying: (token?: string) =>
      get<{ results: MediaItem[] }>("/media/now-playing", {}, token),

    upcomingMovies: (token?: string) =>
      get<{ results: MediaItem[] }>("/media/upcoming", {}, token),

    onAirThisWeek: (token?: string) =>
      get<{ results: MediaItem[] }>("/media/on-air-this-week", {}, token),

    hiddenGems: (type: string = "movie", token?: string) =>
      get<{ results: MediaItem[] }>("/media/hidden-gems", { type }, token),

    recommended: (token?: string) =>
      get<{ results: MediaItem[] }>("/media/recommended", {}, token),

    forYou: (token?: string) =>
      get<{ results: MediaItem[] }>("/media/for-you", {}, token),

    collect: (body: { tmdb_id: number; media_type: string }, token: string) =>
      post<{ status: string; message: string }>("/media/collect", body, token),

    request: (type: string, tmdbId: number, token: string) =>
      post<{ status: string; movie?: any; series?: any }>(`/media/${type}/${tmdbId}/request`, undefined, token),

    uncollect: (tmdbId: number, mediaType: string, token: string) =>
      del<{ status: string }>(`/media/collect?tmdb_id=${tmdbId}&media_type=${mediaType}`, token),

    refreshMovie: (tmdbId: number, token: string) =>
      post<{ message: string }>(`/media/movie/${tmdbId}/refresh`, undefined, token),
  },

  shows: {
    list: (params?: { sort?: string; page?: number; page_size?: number; genre?: string; year?: number; status?: string }, token?: string) =>
      get<{ page: number; page_size: number; total_results: number; total_pages: number; results: any[] }>("/shows", params, token),

    get: (seriesTmdbId: number, token?: string) =>
      get<Show>(`/shows/${seriesTmdbId}`, undefined, token),

    getRecommendations: (seriesTmdbId: number, token?: string) =>
      get<{ results: MediaItem[] }>(`/shows/${seriesTmdbId}/recommendations`, undefined, token),

    getSeason: (seriesTmdbId: number, seasonNumber: number, token?: string) =>
      get<Season>(`/shows/${seriesTmdbId}/season/${seasonNumber}`, undefined, token),

    getEpisode: (seriesTmdbId: number, seasonNumber: number, episodeNumber: number, token?: string) =>
      get<EpisodeDetail>(`/shows/${seriesTmdbId}/season/${seasonNumber}/${episodeNumber}`, undefined, token),

    refreshMetadata: (seriesTmdbId: number, token: string) =>
      post<{ message: string }>(`/shows/${seriesTmdbId}/refresh`, undefined, token),
  },

  history: {
    list: (params?: { page?: number; page_size?: number; type?: string }, token?: string) =>
      get<{ page: number; page_size: number; total_pages: number; total_results: number; results: WatchEvent[] }>("/history", params, token),

    markAsWatched: (body: { tmdb_id: number; media_type: string; watched_at?: string; completed?: boolean }, token: string) =>
      post<{ message: string }>("/history", body, token),

    unwatchItem: (tmdbId: number, mediaType: string, token: string) =>
      del<{ status: string }>(`/history/item?tmdb_id=${tmdbId}&media_type=${mediaType}`, token),

    markSeasonWatched: (body: { series_tmdb_id: number; season_number: number }, token: string) =>
      post<{ status: string; count: number }>("/history/season", body, token),

    unmarkSeasonWatched: (seriesTmdbId: number, seasonNumber: number, token: string) =>
      del<{ status: string }>(`/history/season?series_tmdb_id=${seriesTmdbId}&season_number=${seasonNumber}`, token),

    markShowWatched: (body: { series_tmdb_id: number }, token: string) =>
      post<{ status: string; count: number }>("/history/show-all", body, token),

    unmarkShowWatched: (seriesTmdbId: number, token: string) =>
      del<{ status: string }>(`/history/show-all?series_tmdb_id=${seriesTmdbId}`, token),

    continueWatching: (token?: string) =>
      get<{ continue_watching: ContinueWatchingItem[] }>("/history/continue-watching", undefined, token),

    nextUp: (token?: string, limit?: number) =>
      get<{ next_up: MediaItem[] }>("/history/next-up", limit ? { limit } : undefined, token),

    nowPlaying: (token: string) =>
      get<{ now_playing: NowPlayingSession[] }>("/history/now-playing", undefined, token),
  },

  lists: {
    getAll: (token: string) =>
      get<{ lists: UserList[] }>("/lists", undefined, token),
    getPublic: (token: string) =>
      get<{ lists: PublicList[] }>("/lists/public", undefined, token),
    create: (body: { name: string; description?: string; privacy_level?: PrivacyLevel }, token: string) =>
      post<UserList>("/lists", body, token),
    get: (id: number, token: string) =>
      get<ListDetail>(`/lists/${id}`, undefined, token),
    update: (id: number, body: { name?: string; description?: string; privacy_level?: PrivacyLevel }, token: string) =>
      patch<UserList>(`/lists/${id}`, body, token),
    delete: (id: number, token: string) =>
      del<{ message: string }>(`/lists/${id}`, token),
    addItem: (listId: number, body: { tmdb_id: number; media_type: string }, token: string) =>
      post<ListItemEntry>(`/lists/${listId}/items`, body, token),
    removeItem: (listId: number, itemId: number, token: string) =>
      del<{ message: string }>(`/lists/${listId}/items/${itemId}`, token),
  },

  sync: {
    jellyfin: (params?: { movie_limit?: number; show_limit?: number }, token?: string) =>
      post<{ status: string; stats: Record<string, unknown> }>("/sync/jellyfin", params, token),
    plex: (params?: { movie_limit?: number; show_limit?: number }, token?: string) =>
      post<{ status: string; stats: Record<string, unknown> }>("/sync/plex", params, token),
    status: (token: string) =>
      get<SyncJob[]>("/sync/status", undefined, token),
    jellyfinLibraries: (token: string) =>
      get<{ libraries: { id: string; name: string; type: string; selected: boolean }[]; all_selected: boolean }>("/sync/jellyfin/libraries", undefined, token),
    saveJellyfinLibraries: (library_ids: string[], token: string) =>
      put<{ saved: number }>("/sync/jellyfin/libraries", { library_ids }, token),
    emby: (params?: { movie_limit?: number; show_limit?: number }, token?: string) =>
      post<{ status: string; stats: Record<string, unknown> }>("/sync/emby", params, token),
    embyLibraries: (token: string) =>
      get<{ libraries: { id: string; name: string; type: string; selected: boolean }[]; all_selected: boolean }>("/sync/emby/libraries", undefined, token),
    saveEmbyLibraries: (library_ids: string[], token: string) =>
      put<{ saved: number }>("/sync/emby/libraries", { library_ids }, token),
    plexLibraries: (token: string) =>
      get<{ libraries: { key: string; name: string; type: string; selected: boolean }[]; all_selected: boolean }>("/sync/plex/libraries", undefined, token),
    savePlexLibraries: (library_keys: string[], token: string) =>
      put<{ saved: number }>("/sync/plex/libraries", { library_keys }, token),
  },

  profile: {
    get: (token: string) =>
      get<UserPreferences>("/profile/me", undefined, token),
    getPublic: (userId: number, token?: string) =>
      get<PublicProfile>(`/profile/${userId}`, undefined, token),
    update: (body: Partial<UserPreferences>, token: string) =>
      patch<UserPreferences>("/profile/me", body, token),
    uploadAvatar: (formData: FormData, token: string) =>
      request<{ avatar_url: string }>("/profile/me/avatar", "POST", undefined, formData, token),
    deleteAvatar: (token: string) =>
      del<{ status: string }>("/profile/me/avatar", token),
    follow: (userId: number, token: string) =>
      post<{ status: string }>(`/profile/${userId}/follow`, undefined, token),
    unfollow: (userId: number, token: string) =>
      del<{ status: string }>(`/profile/${userId}/follow`, token),
    searchUsers: (q: string, token?: string) =>
      get<{ results: UserSearchResult[] }>("/profile/search", { q }, token),
    getStats: (userId: number, token?: string) =>
      get<UserStats>(`/profile/${userId}/stats`, undefined, token),
  },

  comments: {
    list: (params: { media_type: string; tmdb_id: number; season_number?: number; episode_number?: number }, token?: string) =>
      get<Comment[]>("/comments", params, token),
    create: (body: { media_type: string; tmdb_id: number; season_number?: number; episode_number?: number; content: string }, token: string) =>
      post<Comment>("/comments", body, token),
    update: (id: number, content: string, token: string) =>
      patch<{ id: number; content: string; updated_at: string | null }>(`/comments/${id}`, { content }, token),
    delete: (id: number, token: string) =>
      del<{ message: string }>(`/comments/${id}`, token),
  },
};

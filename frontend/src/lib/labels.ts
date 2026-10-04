// Display labels for values that stay in English on the wire: TMDB genre and
// show-status names are sent to the backend as filter values, so only what the
// user sees is translated. Unknown values (e.g. genres TMDB already returned in
// the metadata language) are shown as-is.
import { m } from "../paraglide/messages.js";
import { baseLocale, getLocale } from "../paraglide/runtime.js";

const GENRES: Record<string, () => string> = {
  "Action": m.genre_action,
  "Adventure": m.genre_adventure,
  "Action & Adventure": m.genre_action_adventure,
  "Animation": m.genre_animation,
  "Comedy": m.genre_comedy,
  "Crime": m.genre_crime,
  "Documentary": m.genre_documentary,
  "Drama": m.genre_drama,
  "Family": m.genre_family,
  "Fantasy": m.genre_fantasy,
  "History": m.genre_history,
  "Horror": m.genre_horror,
  "Kids": m.genre_kids,
  "Music": m.genre_music,
  "Mystery": m.genre_mystery,
  "News": m.genre_news,
  "Reality": m.genre_reality,
  "Romance": m.genre_romance,
  "Science Fiction": m.genre_science_fiction,
  "Sci-Fi & Fantasy": m.genre_scifi_fantasy,
  "Soap": m.genre_soap,
  "Talk": m.genre_talk,
  "Thriller": m.genre_thriller,
  "TV Movie": m.genre_tv_movie,
  "War": m.genre_war,
  "War & Politics": m.genre_war_politics,
  "Western": m.genre_western,
};

export function genreLabel(name: string): string {
  return GENRES[name]?.() ?? name;
}

const SHOW_STATUSES: Record<string, () => string> = {
  "Returning Series": m.show_status_returning,
  "In Production": m.show_status_in_production,
  "Planned": m.show_status_planned,
  "Ended": m.show_status_ended,
  "Canceled": m.show_status_canceled,
  "Cancelled": m.show_status_canceled,
  "Pilot": m.show_status_pilot,
};

export function showStatusLabel(status: string): string {
  return SHOW_STATUSES[status]?.() ?? status;
}

// Separate from show statuses: French agrees the adjective with the noun
// (un film "sorti", une série "terminée").
const MOVIE_STATUSES: Record<string, () => string> = {
  "Released": m.movie_status_released,
  "Post Production": m.movie_status_post_production,
  "In Production": m.movie_status_in_production,
  "Planned": m.movie_status_planned,
  "Rumored": m.movie_status_rumored,
  "Canceled": m.movie_status_canceled,
  "Cancelled": m.movie_status_canceled,
};

export function movieStatusLabel(status: string): string {
  return MOVIE_STATUSES[status]?.() ?? status;
}

// Crew jobs: the backend keeps only these (NOTABLE_CREW_JOBS in core/tmdb.py,
// CREW_PEOPLE_TYPES in core/tvdb.py) and joins a person's roles with ", "
// (a writer-director is one entry), so each part is translated on its own.
const CREW_JOBS: Record<string, () => string> = {
  "Director": m.crew_job_director,
  "Writer": m.crew_job_writer,
  "Screenplay": m.crew_job_screenplay,
  "Story": m.crew_job_story,
  "Teleplay": m.crew_job_teleplay,
  "Producer": m.crew_job_producer,
  "Executive Producer": m.crew_job_executive_producer,
  "Co-Producer": m.crew_job_co_producer,
};

export function crewJobLabel(job: string): string {
  // Deduped after translating: "Writer" and "Screenplay" can share a label.
  return [...new Set(job.split(", ").map((part) => CREW_JOBS[part]?.() ?? part))].join(", ");
}

const capitalize = (s: string) => s.charAt(0).toLocaleUpperCase(getLocale()) + s.slice(1);

// Language and country names come from the runtime's CLDR data (Intl), so they
// need no catalog entries. French returns "français"/"allemand" in lowercase,
// which reads wrong as a standalone option label - hence the capitalization.
export function languageName(code: string): string {
  try {
    return capitalize(new Intl.DisplayNames([getLocale()], { type: "language", languageDisplay: "standard" }).of(code) ?? code);
  } catch {
    return code;
  }
}

export function countryName(code: string): string {
  try {
    return new Intl.DisplayNames([getLocale()], { type: "region" }).of(code) ?? code;
  } catch {
    return code;
  }
}

// Sorts option labels in the UI language's alphabetical order.
export function sortByLabel<T extends { label: string }>(options: T[]): T[] {
  return [...options].sort((a, b) => a.label.localeCompare(b.label, getLocale()));
}

// For curated option lists that already carry an English label: keep it
// verbatim in English (some differ from CLDR, e.g. "Hong Kong" rather than
// "Hong Kong SAR China") and use the CLDR name in any other UI language.
export function localizedLanguageName(code: string, englishName: string): string {
  return getLocale() === baseLocale ? englishName : languageName(code);
}

export function localizedCountryName(code: string, englishName: string): string {
  return getLocale() === baseLocale ? englishName : countryName(code);
}

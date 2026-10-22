// Locale-aware date/number formatting for the active UI language. Works in SSR
// frontmatter and in client <script>s alike: getLocale() reads the per-request
// locale on the server and the ui_language cookie / navigator.languages in the
// browser. Use these instead of toLocale*String('en-US') or an undefined locale,
// which would follow the server's or browser's locale rather than the UI's.
import { getLocale } from "../paraglide/runtime.js";
import { m } from "../paraglide/messages.js";

type DateInput = Date | string | number;

const toDate = (d: DateInput) => (d instanceof Date ? d : new Date(d));

// Dates and numbers follow the viewer's region as well as the UI language. UI
// locales are bare languages, which Intl reads with a default region ("en" as
// en-US): on its own, that would give a British viewer - or one whose language
// isn't shipped and falls back to English - 10/3/2026 instead of 03/10/2026,
// where the browser's own locale used to apply. So the region of the viewer's
// first preferred language is grafted onto the UI language: en-GB, en-DE
// (03/10/2026, 24h), fr-CA (2026-10-03). A pair Intl has no data for (en-BR)
// resolves to the bare language. The first preferred language is
// navigator.languages[0] in the browser and the first Accept-Language entry on
// the server - the same tag, so SSR and client-rendered dates agree.
let serverRegion: () => string | undefined = () => undefined;

/** Hands the server the current request's region: navigator doesn't exist there (see lib/ui-locale.ts). */
export function setServerRegionResolver(resolve: () => string | undefined): void {
  serverRegion = resolve;
}

/** The region subtag of a language tag ("en-GB" -> "GB"), inferred when it has none ("de" -> "DE"). */
export function regionOf(tag: string | undefined): string | undefined {
  if (!tag) return undefined;
  try {
    return new Intl.Locale(tag).maximize().region;
  } catch {
    return undefined; // "*" or a malformed tag
  }
}

let browserRegion: string | undefined | null = null;

function viewerRegion(): string | undefined {
  if (typeof window === "undefined") return serverRegion();
  if (browserRegion === null) browserRegion = regionOf(navigator.languages?.[0] ?? navigator.language);
  return browserRegion;
}

/** The UI language combined with the viewer's region ("en-GB"), for date and number formats. */
export function formatLocale(): string {
  const language = getLocale();
  const region = viewerRegion();
  if (!region) return language;
  try {
    return new Intl.Locale(language, { region }).baseName;
  } catch {
    return language;
  }
}

export function formatDate(date: DateInput, options?: Intl.DateTimeFormatOptions): string {
  return toDate(date).toLocaleDateString(formatLocale(), options);
}

export function formatTime(date: DateInput, options?: Intl.DateTimeFormatOptions): string {
  return toDate(date).toLocaleTimeString(formatLocale(), options);
}

export function formatDateTime(date: DateInput, options?: Intl.DateTimeFormatOptions): string {
  return toDate(date).toLocaleString(formatLocale(), options);
}

export function formatNumber(n: number, options?: Intl.NumberFormatOptions): string {
  return n.toLocaleString(formatLocale(), options);
}

// Ratings (TMDB's 0-10, the user's half-star steps) always show one decimal,
// with the locale's separator: 8.5 in English, 8,5 in French.
export function formatRating(value: number | string): string {
  return formatNumber(Number(value), { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

export function formatRelative(value: number, unit: Intl.RelativeTimeFormatUnit, options?: Intl.RelativeTimeFormatOptions): string {
  return new Intl.RelativeTimeFormat(getLocale(), { numeric: "auto", ...options }).format(value, unit);
}

export function formatList(items: string[], options?: Intl.ListFormatOptions): string {
  return new Intl.ListFormat(getLocale(), { style: "long", type: "conjunction", ...options }).format(items);
}

// For messages that carry inline markup (a link or <strong> around part of a
// sentence, rendered with set:html): the markup lives in the message so
// translators keep the sentence whole, and every interpolated value that isn't
// trusted markup goes through this first.
export function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Compact "5m ago" / "3d ago" style age, as shown on profile activity rows.
export function formatTimeAgo(date: DateInput): string {
  const diff = Date.now() - toDate(date).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return m.time_ago_minutes({ count: mins });
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return m.time_ago_hours({ count: hrs });
  const days = Math.floor(hrs / 24);
  if (days < 30) return m.time_ago_days({ count: days });
  const months = Math.floor(days / 30);
  if (months < 12) return m.time_ago_months({ count: months });
  return m.time_ago_years({ count: Math.floor(months / 12) });
}

// File sizes in binary units with localized unit symbols (GB / Go).
export function formatBytes(bytes: number): string {
  const fixed = (value: number, digits: number) =>
    formatNumber(value, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  if (bytes >= 1073741824) return m.unit_gigabytes({ value: fixed(bytes / 1073741824, 2) });
  if (bytes >= 1048576) return m.unit_megabytes({ value: fixed(bytes / 1048576, 1) });
  if (bytes >= 1024) return m.unit_kilobytes({ value: fixed(bytes / 1024, 0) });
  return m.unit_bytes({ value: bytes });
}

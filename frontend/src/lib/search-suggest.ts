// Search suggestions dropdown for the global nav search boxes (#536).
//
// A follow-up to the live search on /search itself (#367, see search.astro's
// setupLiveSearch): on every other page, typing in the nav box shows the top
// few matches under it, so a title can be opened directly without loading the
// full results page. Pressing Enter with no suggestion highlighted still
// submits the form to /search exactly as before.
//
// Client-only module: it must not import lib/api.ts, which is the SSR client
// (it talks to the backend on localhost). Requests go through /api/proxy like
// the other client scripts.
import { m } from "../paraglide/messages.js";
import { episodeHref } from "./episodeHref";
import { rankByMatch } from "./search-rank";

const MIN_CHARS = 2;        // the backend's /media/search rejects shorter queries
const DEBOUNCE_MS = 300;
const MAX_SUGGESTIONS = 8;
const MAX_COLLECTIONS = 2;  // at most this many collections, so they can't crowd out titles
const CACHE_SIZE = 30;

interface SuggestItem {
  id?: number | null;
  tmdb_id?: number | null;
  tvdb_id?: number | null;
  type: string;
  title?: string | null;
  original_title?: string | null;
  poster_path?: string | null;
  release_date?: string | null;
  adult?: boolean;
  in_library?: boolean;
  collection_pct?: number;
  watched?: boolean;
  watch_pct?: number;
  season_number?: number | null;
  [key: string]: unknown;
}

interface Suggestion {
  item: SuggestItem;
  href: string;
}

declare global {
  interface Window { __AUTH_TOKEN__?: string }
}

function authHeaders(): HeadersInit {
  const token = window.__AUTH_TOKEN__;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function suggestionHref(item: SuggestItem): string | null {
  if (item.type === "person") return item.tmdb_id ? `/person/${item.tmdb_id}` : null;
  if (item.type === "collection") return item.tmdb_id ? `/collection/${item.tmdb_id}` : null;
  if (item.type === "movie" || item.type === "series") return episodeHref(item);
  return null;
}

// Small thumbnails: request TMDB's w92 size through the image proxy rather
// than the w500 the search results carry. Mirrors tmdbImageUrl in lib/api.ts.
function thumbUrl(path: string | null | undefined): string | null {
  if (!path) return null;
  if (path.startsWith("http://") || path.startsWith("https://")) {
    const tmdb = /image\.tmdb\.org\/t\/p\/[^/]+(\/.+)$/.exec(path);
    if (tmdb) return `/api/proxy/media/image/w92${tmdb[1]}`;
    const tvdb = /artworks\.thetvdb\.com(\/.+)$/.exec(path);
    if (tvdb) return `/api/proxy/media/image/tvdb${tvdb[1]}`;
    return path;
  }
  return `/api/proxy/media/image/w92${path.startsWith("/") ? path : `/${path}`}`;
}

function typeLabel(type: string): string {
  switch (type) {
    case "movie": return m.common_movie();
    case "series": return m.common_show();
    case "person": return m.common_person();
    case "collection": return m.common_collection();
    default: return "";
  }
}

// Same derivation as MediaCard.astro, so a badge here never disagrees with
// the card on the results page.
function isCollected(item: SuggestItem): boolean {
  return (item.collection_pct ?? (item.in_library ? 100 : 0)) > 0;
}
function isWatched(item: SuggestItem): boolean {
  return (item.watch_pct ?? (item.watched ? 100 : 0)) >= 100;
}

const PLACEHOLDER_ICONS: Record<string, string> = {
  person: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  series: '<rect width="20" height="15" x="2" y="7" rx="2"/><polyline points="17 2 12 7 7 2"/>',
  collection: '<path d="m12 2 10 5-10 5L2 7l10-5Z"/><path d="m2 12 10 5 10-5"/><path d="m2 17 10 5 10-5"/>',
  default: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M7 3v18M17 3v18M3 7.5h4M3 16.5h4M17 7.5h4M17 16.5h4M3 12h18"/>',
};

function placeholder(type: string): HTMLElement {
  const el = document.createElement("div");
  el.className = "w-full h-full flex items-center justify-center text-zinc-600";
  el.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="55%" height="55%" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PLACEHOLDER_ICONS[type] ?? PLACEHOLDER_ICONS.default}</svg>`;
  return el;
}

function badge(text: string, cls: string): HTMLElement {
  const el = document.createElement("span");
  el.className = `shrink-0 text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded-full ${cls}`;
  el.textContent = text;
  return el;
}

// Shared across both boxes: the same query typed on desktop and then on the
// mobile bar (e.g. after a resize) needn't hit TMDB again.
const cache = new Map<string, Suggestion[]>();

async function fetchSuggestions(q: string, signal: AbortSignal): Promise<Suggestion[]> {
  const cached = cache.get(q);
  if (cached) return cached;

  const base = `/api/proxy/media/search?q=${encodeURIComponent(q)}`;
  const get = async (url: string): Promise<SuggestItem[]> => {
    const res = await fetch(url, { headers: authHeaders(), signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return Array.isArray(data?.results) ? data.results : [];
  };
  // "All" covers movies, shows and people; collections are a separate search.
  // A failed collection search shouldn't sink the main suggestions.
  const [main, collections] = await Promise.all([
    get(base),
    get(`${base}&type=collection`).catch((e) => {
      if ((e as Error).name === "AbortError") throw e;
      return [] as SuggestItem[];
    }),
  ]);

  // Rank everything together by how well the title matches what was typed;
  // the backend's popularity order breaks ties. Collections come last in the
  // input, so on a tie a movie, show or person goes first.
  const picked: Suggestion[] = [];
  let collectionCount = 0;
  for (const item of rankByMatch([...main, ...collections], q)) {
    if (picked.length >= MAX_SUGGESTIONS) break;
    const href = suggestionHref(item);
    if (!href || !item.title) continue;
    if (item.type === "collection") {
      if (collectionCount >= MAX_COLLECTIONS) continue;
      collectionCount++;
    }
    picked.push({ item, href });
  }

  cache.set(q, picked);
  if (cache.size > CACHE_SIZE) cache.delete(cache.keys().next().value!);
  return picked;
}

let instanceCount = 0;

function attach(input: HTMLInputElement, panel: HTMLElement, setLoading: (on: boolean) => void) {
  const listId = panel.id || `search-suggest-${++instanceCount}`;
  panel.id = listId;
  panel.setAttribute("role", "listbox");
  panel.setAttribute("aria-label", m.search_suggest_label());
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", listId);
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("autocomplete", "off");

  const loggedIn = !!window.__AUTH_TOKEN__;
  let debounceTimer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  let options: HTMLAnchorElement[] = [];
  let active = -1;
  let shownQuery = "";

  const isOpen = () => !panel.classList.contains("hidden");

  function setActive(index: number) {
    options[active]?.classList.remove("bg-zinc-800");
    options[active]?.setAttribute("aria-selected", "false");
    active = index;
    const el = options[active];
    if (el) {
      el.classList.add("bg-zinc-800");
      el.setAttribute("aria-selected", "true");
      el.scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", el.id);
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function close() {
    if (!isOpen()) return;
    panel.classList.add("hidden");
    input.setAttribute("aria-expanded", "false");
    setActive(-1);
  }

  function open() {
    if (isOpen() || !panel.hasChildNodes()) return;
    panel.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
  }

  function option(href: string, index: number): HTMLAnchorElement {
    const a = document.createElement("a");
    a.href = href;
    a.id = `${listId}-opt-${index}`;
    a.setAttribute("role", "option");
    a.setAttribute("aria-selected", "false");
    a.tabIndex = -1;
    a.addEventListener("mouseenter", () => setActive(index));
    return a;
  }

  function render(q: string, suggestions: Suggestion[]) {
    panel.replaceChildren();
    options = [];
    active = -1;
    input.removeAttribute("aria-activedescendant");
    shownQuery = q;

    if (suggestions.length === 0) {
      const empty = document.createElement("p");
      empty.className = "px-3 py-3 text-sm text-zinc-500";
      empty.textContent = m.search_no_results({ query: q });
      panel.append(empty);
    }

    suggestions.forEach(({ item, href }, i) => {
      const a = option(href, i);
      a.className = "flex items-center gap-3 px-2 py-1.5 rounded-lg text-zinc-100 transition-colors";

      const thumb = document.createElement("div");
      // Posters are 2:3; a person's profile photo goes in a circle the same width.
      thumb.className = `relative w-9 shrink-0 overflow-hidden bg-zinc-950 border border-zinc-800 ${item.type === "person" ? "h-9 rounded-full" : "h-[3.375rem] rounded-md"}`;
      if (item.adult) {
        thumb.dataset.adult = "true";
        thumb.dataset.noLabel = "";
      }
      const src = thumbUrl(item.poster_path);
      if (src) {
        const img = document.createElement("img");
        img.src = src;
        img.alt = "";
        img.loading = "lazy";
        img.decoding = "async";
        img.className = "w-full h-full object-cover";
        img.addEventListener("error", () => img.replaceWith(placeholder(item.type)), { once: true });
        thumb.append(img);
      } else {
        thumb.append(placeholder(item.type));
      }

      const text = document.createElement("div");
      text.className = "flex-1 min-w-0";
      const title = document.createElement("p");
      title.className = "text-sm font-semibold truncate";
      title.textContent = item.title ?? "";
      const meta = document.createElement("p");
      meta.className = "text-xs text-zinc-500 truncate";
      const year = item.type !== "person" ? item.release_date?.slice(0, 4) : null;
      meta.textContent = [typeLabel(item.type), year].filter(Boolean).join(" · ");
      text.append(title, meta);

      a.append(thumb, text);

      // Library/watched badges only mean something for a signed-in user -
      // anonymous search runs against the shared anonymous account.
      if (loggedIn && (item.type === "movie" || item.type === "series")) {
        if (isWatched(item)) a.append(badge(m.calendar_badge_watched(), "bg-green-500/20 text-green-400"));
        else if (isCollected(item)) a.append(badge(m.calendar_badge_collected(), "bg-purple-500/20 text-purple-400"));
      }

      options.push(a);
      panel.append(a);
    });

    const all = option(`/search?q=${encodeURIComponent(q)}`, options.length);
    all.className = "block mt-1 px-3 py-2 rounded-lg text-sm font-semibold text-blue-400 border-t border-zinc-800 transition-colors";
    all.textContent = m.search_suggest_see_all({ query: q });
    options.push(all);
    panel.append(all);

    if (document.activeElement === input) {
      panel.classList.remove("hidden");
      input.setAttribute("aria-expanded", "true");
    }
  }

  async function run(q: string) {
    controller?.abort();
    controller = new AbortController();
    const { signal } = controller;
    setLoading(true);
    try {
      const suggestions = await fetchSuggestions(q, signal);
      // The box may have changed while this was in flight.
      if (input.value.trim() === q) render(q, suggestions);
    } catch (e) {
      if ((e as Error).name === "AbortError") return; // superseded; its replacement owns the spinner
      console.error("Search suggestions failed:", e);
      close();
    }
    setLoading(false);
  }

  input.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    const q = input.value.trim();
    if (q.length < MIN_CHARS) {
      controller?.abort();
      setLoading(false);
      close();
      panel.replaceChildren();
      shownQuery = "";
      return;
    }
    if (q === shownQuery) {
      // Back to what's already rendered (e.g. typed a letter, then deleted it).
      controller?.abort();
      setLoading(false);
      open();
      return;
    }
    debounceTimer = setTimeout(() => run(q), DEBOUNCE_MS);
  });

  input.addEventListener("focus", () => {
    if (input.value.trim() === shownQuery && shownQuery) open();
  });

  input.addEventListener("keydown", (e) => {
    if (e.isComposing) return;
    switch (e.key) {
      case "ArrowDown":
        if (!isOpen()) { open(); if (!isOpen()) return; }
        e.preventDefault();
        setActive(active + 1 >= options.length ? 0 : active + 1);
        break;
      case "ArrowUp":
        if (!isOpen()) return;
        e.preventDefault();
        setActive(active <= 0 ? options.length - 1 : active - 1);
        break;
      case "Enter":
        // Nothing highlighted: fall through to the normal form submit, which
        // opens the full results page as it always has.
        if (isOpen() && options[active]) {
          e.preventDefault();
          window.location.href = options[active].href;
        }
        break;
      case "Escape":
        // Close the dropdown first; a second Esc then reaches Base.astro's
        // handler that closes the mobile search bar.
        if (isOpen()) {
          e.preventDefault();
          e.stopPropagation();
          close();
        }
        break;
      case "Tab":
        close();
        break;
    }
  });

  // Submitting (Enter or the mobile "Go" button) navigates away anyway; drop
  // any pending lookup so it can't pop the dropdown back open meanwhile.
  input.closest("form")?.addEventListener("submit", () => {
    clearTimeout(debounceTimer);
    controller?.abort();
    close();
  });

  // pointerdown, not blur: on touch devices tapping a suggestion link doesn't
  // reliably move focus to it, so a blur handler would close the panel
  // before the tap registers.
  document.addEventListener("pointerdown", (e) => {
    const target = e.target as Node;
    if (!panel.contains(target) && target !== input) close();
  });
}

export function initSearchSuggest() {
  // /search re-runs its own results in place as you type (search.astro);
  // a dropdown on top of that would just duplicate them.
  if (window.location.pathname === "/search") return;

  // Same loading cues search.astro's live search uses: the "/" hint swaps for
  // a spinner on desktop, and a spinner shows inside the mobile box.
  const desktopHint = document.getElementById("desktop-search-hint");
  const desktopSpinner = document.getElementById("desktop-search-spinner");
  const mobileSpinner = document.getElementById("mobile-search-spinner");

  const desktopInput = document.getElementById("desktop-search-input");
  const desktopPanel = document.getElementById("desktop-search-suggest");
  if (desktopInput instanceof HTMLInputElement && desktopPanel) {
    attach(desktopInput, desktopPanel, (on) => {
      desktopHint?.classList.toggle("hidden", on);
      desktopSpinner?.classList.toggle("hidden", !on);
    });
  }

  const mobileInput = document.getElementById("mobile-search-input");
  const mobilePanel = document.getElementById("mobile-search-suggest");
  if (mobileInput instanceof HTMLInputElement && mobilePanel) {
    attach(mobileInput, mobilePanel, (on) => {
      mobileSpinner?.classList.toggle("hidden", !on);
    });
  }
}

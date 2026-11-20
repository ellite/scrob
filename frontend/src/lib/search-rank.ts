// Ranking for the nav search suggestions (#536).
//
// The backend's "All" search merges TMDB's movie, show and people results by
// popularity alone, so a popular result that only loosely matches the query
// (TMDB also matches alternative titles, overviews' keywords, etc.) can push
// real matches out of a short suggestions list. This puts titles with a word
// starting with the query first, keeping the incoming order (popularity)
// within each level. People go after all titles.
//
// No imports, so it can be unit-tested directly (test/search-rank.test.mjs).

export interface RankableItem {
  type: string;
  title?: string | null;
  original_title?: string | null;
  in_library?: boolean;
  collection_pct?: number;
  watched?: boolean;
  watch_pct?: number;
}

// Lowercase, strip accents, and turn punctuation into spaces, so "Amélie" and
// "amelie", or "Spider-Man" and "spider man", compare equal.
export function normalizeForMatch(s: string): string {
  return s
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

// 2 a word in the title starts with the query (the title itself, or any word
// in it), 1 it appears mid-word, 0 no title match (TMDB matched on something
// else, e.g. an alternative title). The best of title and original title
// counts, so a query in the original language ranks too.
//
// An exact or whole-title-prefix match deliberately gets no extra credit: the
// box is searched while typing, so "spid" is usually on its way to "Spider-Man",
// and rewarding the exact match would lift obscure titles named "Spid" over
// it. Among word-start matches the backend's popularity order decides.
export function matchLevel(item: RankableItem, normalizedQuery: string): number {
  if (!normalizedQuery) return 0;
  let best = 0;
  for (const raw of [item.title, item.original_title]) {
    if (typeof raw !== "string" || !raw) continue;
    const t = normalizeForMatch(raw);
    let level = 0;
    if (` ${t}`.includes(` ${normalizedQuery}`)) level = 2;
    else if (t.includes(normalizedQuery)) level = 1;
    if (level > best) best = level;
  }
  return best;
}

// Something the user owns or has watched: a nudge within a match level only,
// never enough to lift an item over a better title match.
function isPersonal(item: RankableItem): boolean {
  if (item.type !== "movie" && item.type !== "series") return false;
  return (
    (item.collection_pct ?? (item.in_library ? 100 : 0)) > 0 ||
    (item.watch_pct ?? (item.watched ? 100 : 0)) > 0
  );
}

// People always come after movies, shows and collections, however well the
// name matches; each group is then ordered by score. Stable: equal scores keep
// their incoming order.
export function rankByMatch<T extends RankableItem>(items: T[], query: string): T[] {
  const q = normalizeForMatch(query);
  return items
    .map((item, index) => ({
      item,
      index,
      group: item.type === "person" ? 1 : 0,
      score: matchLevel(item, q) * 2 + (isPersonal(item) ? 1 : 0),
    }))
    .sort((a, b) => a.group - b.group || b.score - a.score || a.index - b.index)
    .map(({ item }) => item);
}

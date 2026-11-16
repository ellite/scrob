// Sort direction for SortButton: picking the active sort again flips it.
// Every sort keeps its natural default (title A-Z, everything else
// newest/highest first), so pages behave exactly as before until flipped.

export type SortDir = "asc" | "desc";

// Sorts whose natural order is ascending; everything else starts descending.
const ASCENDING_SORTS = new Set(["title"]);

export function defaultDir(sort: string): SortDir {
  return ASCENDING_SORTS.has(sort) ? "asc" : "desc";
}

export function parseDir(value: string | null | undefined): SortDir | undefined {
  return value === "asc" || value === "desc" ? value : undefined;
}

export function effectiveDir(sort: string, dir: string | null | undefined): SortDir {
  return parseDir(dir) ?? defaultDir(sort);
}

export function flipDir(dir: SortDir): SortDir {
  return dir === "asc" ? "desc" : "asc";
}

/** What picking `value` in the sort menu leads to: the active sort flips,
 * another sort starts in its own default direction. */
export function nextSort(current: string, currentDir: string | null | undefined, value: string): { sort: string; dir: SortDir } {
  if (value === current) return { sort: value, dir: flipDir(effectiveDir(current, currentDir)) };
  return { sort: value, dir: defaultDir(value) };
}

/** The arrow shown on the sort button and beside the active menu entry. */
export function dirArrow(dir: SortDir): string {
  return dir === "asc" ? "↑" : "↓";
}

/** Accessible wording for the arrow (title/aria-label). */
export function dirLabel(dir: SortDir): string {
  return dir === "asc" ? "ascending" : "descending";
}

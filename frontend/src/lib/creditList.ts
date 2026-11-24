// Search helpers for the full crew list on a title page.

// Lower-cases and strips accents so "cuaron" finds "Cuarón".
function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase();
}

// The text a crew row is searched by: the person's name plus their job labels.
export function creditSearchText(...parts: (string | null | undefined)[]): string {
  return fold(parts.filter(Boolean).join(" "));
}

// Every word of the query must appear somewhere in the row text; an empty
// query matches everything.
export function matchesCreditQuery(searchText: string, query: string): boolean {
  const words = fold(query).split(/\s+/).filter(Boolean);
  return words.every((word) => searchText.includes(word));
}

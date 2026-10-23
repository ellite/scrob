// The "My Rating" filter (#136): whole-number values 1-10, shared by the Explore
// and Collection pages for movies and shows.
export const MY_RATING_OPTIONS = Array.from({ length: 10 }, (_, index) => 10 - index).map((rating) => ({
  value: String(rating), label: `${rating} / 10`,
}));

export function parseMyRating(url: URL): number[] {
  return url.searchParams.getAll("my_rating").map(Number).filter((n) => Number.isInteger(n) && n >= 1 && n <= 10);
}

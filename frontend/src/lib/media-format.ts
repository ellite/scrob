export function formatSeasonTitle(seasonNumber: number, name?: string | null): string {
  const fallback = `Season ${seasonNumber}`;
  const trimDecorators = (value: string) => value.replace(/^[-–—:·\s]+|[-–—:·\s]+$/g, "").trim();
  const normalized = trimDecorators(name ?? "").replace(
    new RegExp(`^season\\s+${seasonNumber}\\s*[-–—:·]?\\s*`, "i"),
    "",
  );
  const customName = trimDecorators(normalized);
  return customName ? `${fallback} · ${customName}` : fallback;
}

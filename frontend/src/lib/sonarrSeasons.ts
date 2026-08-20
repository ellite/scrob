export function selectableSonarrSeasons(seasons: unknown): number[] {
  if (!Array.isArray(seasons)) return [];

  return [...new Set(
    seasons
      .map((season) => (typeof season === 'object' && season !== null ? (season as { season_number?: unknown }).season_number : undefined))
      .filter((number): number is number => Number.isInteger(number) && number >= 0),
  )].sort((a, b) => a - b);
}

export function selectedSonarrSeasons(selected: boolean, checkedSeasons: number[]): number[] | undefined {
  if (!selected) return undefined;
  const seasons = [...new Set(checkedSeasons)];
  if (seasons.length === 0) {
    throw new Error('Select at least one season or choose all seasons.');
  }
  if (seasons.some((season) => !Number.isInteger(season) || season < 0)) {
    throw new Error('Selected seasons must be valid non-negative numbers.');
  }
  return seasons;
}

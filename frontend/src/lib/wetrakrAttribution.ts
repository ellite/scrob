// WeTrakr requires every comment from its API to credit the author and the
// platform: a logo/credit for the comment's `source` plus a link to the
// comment on wetrakr.com ("TV Time review from WeTrakr", or "Review from
// WeTrakr" for one written there).
const SOURCE_NAMES: Record<string, string> = {
  tvtime: "TV Time",
  trakt: "Trakt",
  letterboxd: "Letterboxd",
  imdb: "IMDb",
  simkl: "Simkl",
  tmdb: "TMDB",
};

export function wetrakrCredit(
  source: string | null | undefined,
  commentId: number | null | undefined,
): { label: string; url: string } | null {
  if (!commentId) return null;
  const src = (source || "wetrakr").toLowerCase();
  const name = SOURCE_NAMES[src] ?? src.charAt(0).toUpperCase() + src.slice(1);
  return {
    label: src === "wetrakr" ? "Review from WeTrakr" : `${name} review from WeTrakr`,
    url: `https://wetrakr.com/comments/${commentId}`,
  };
}

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { buildSeasonRatingEntries, summarizeEpisodeRatings } from "../src/lib/season-ratings.mjs";

const chart = await readFile(new URL("../src/components/SeasonRatingChart.astro", import.meta.url), "utf8");

test("season rating chart normalizes ratings and represents missing values", () => {
  assert.deepEqual(
    buildSeasonRatingEntries([
      { episode_number: 3, tmdb_rating: 12, title: "Finale" },
      { episode_number: 1, tmdb_rating: 0, title: "Pilot" },
      { episode_number: 2, tmdb_rating: "8.4", name: "Middle" },
      { episode_number: 4, tmdb_rating: null },
    ]),
    [
      { episodeNumber: 1, title: "Pilot", rating: null },
      { episodeNumber: 2, title: "Middle", rating: 8.4 },
      { episodeNumber: 3, title: "Finale", rating: 10 },
      { episodeNumber: 4, title: null, rating: null },
    ],
  );
  assert.match(chart, /rating-bar--missing/);
  assert.match(chart, /No episode ratings are available for this season yet/);
});

test("season rating chart exposes high and low episodes accessibly", () => {
  const entries = buildSeasonRatingEntries([
    { episode_number: 1, tmdb_rating: 7.5 },
    { episode_number: 2, tmdb_rating: 9.1 },
    { episode_number: 3, tmdb_rating: 6.8 },
  ]);
  const summary = summarizeEpisodeRatings(entries);

  assert.equal(summary.highRating, 9.1);
  assert.equal(summary.lowRating, 6.8);
  assert.equal(summary.highest?.episodeNumber, 2);
  assert.equal(summary.lowest?.episodeNumber, 3);
  assert.equal(summary.hasRange, true);
  assert.match(chart, /rating-bar--highest/);
  assert.match(chart, /rating-bar--lowest/);
  assert.match(chart, /highest rated/);
  assert.match(chart, /lowest rated/);
  assert.match(chart, /aria-label=\{label\}/);
  assert.match(chart, /role="region"/);
  assert.match(chart, /tabindex="0"/);
});

test("both season route families use the shared chart with their own episode links", async () => {
  const tmdb = await readFile(new URL("../src/pages/show/[id]/season/[season_number].astro", import.meta.url), "utf8");
  const tvdb = await readFile(new URL("../src/pages/show/tvdb/[id]/season/[season_number].astro", import.meta.url), "utf8");

  for (const source of [tmdb, tvdb]) {
    assert.match(source, /import SeasonRatingChart/);
    assert.match(source, /<SeasonRatingChart/);
    assert.match(source, /episodes=\{season\.episodes \?\? \[\]\}/);
  }
  assert.match(tmdb, /episodeBasePath=\{`\/show\/\$\{id\}\/season\/\$\{season_number\}`\}/);
  assert.match(tvdb, /episodeBasePath=\{`\/show\/tvdb\/\$\{id\}\/season\/\$\{season_number\}`\}/);
});

import assert from "node:assert/strict";
import test from "node:test";

import { matchLevel, normalizeForMatch, rankByMatch } from "../src/lib/search-rank.ts";

const titles = (items) => items.map((i) => i.title);

test("normalizeForMatch ignores case, accents and punctuation", () => {
  assert.equal(normalizeForMatch("Amélie"), "amelie");
  assert.equal(normalizeForMatch("Spider-Man: No Way Home"), "spider man no way home");
  assert.equal(normalizeForMatch("  WALL·E  "), "wall e");
});

test("matchLevel grades word-start, mid-word and no match", () => {
  const q = normalizeForMatch("dune");
  assert.equal(matchLevel({ type: "movie", title: "Dune" }, q), 2);
  assert.equal(matchLevel({ type: "movie", title: "Dune: Part Two" }, q), 2);
  assert.equal(matchLevel({ type: "movie", title: "Jodorowsky's Dune" }, q), 2);
  assert.equal(matchLevel({ type: "movie", title: "Kodunes" }, q), 1);
  assert.equal(matchLevel({ type: "person", title: "Timothée Chalamet" }, q), 0);
});

test("matchLevel also counts the original title", () => {
  const q = normalizeForMatch("la casa de papel");
  assert.equal(matchLevel({ type: "series", title: "Money Heist", original_title: "La casa de papel" }, q), 2);
});

test("a partial query favours popular titles over obscure exact matches", () => {
  // Incoming order is the backend's popularity order.
  const items = [
    { type: "movie", title: "Spider-Man: Brand New Day" },
    { type: "movie", title: "Spider-Man: No Way Home" },
    { type: "series", title: "Spidey and His Amazing Friends" },
    { type: "movie", title: "The Amazing Spider-Man" },
    { type: "movie", title: "Spid" },
    { type: "movie", title: "SPID" },
  ];
  assert.deepEqual(titles(rankByMatch(items, "spid")), titles(items));
});

test("titles that don't match on a word start drop below those that do", () => {
  const items = [
    { type: "movie", title: "Arachnophobia" }, // TMDB matched something other than the title
    { type: "movie", title: "Despicable Me" }, // "spi" mid-word
    { type: "movie", title: "Spider-Man" },
  ];
  assert.deepEqual(titles(rankByMatch(items, "spi")), ["Spider-Man", "Despicable Me", "Arachnophobia"]);
});

test("rankByMatch puts people after movies, shows and collections, even on a better name match", () => {
  const items = [
    { type: "person", title: "Brad Pitt" },
    { type: "person", title: "Bradley Cooper" },
    { type: "movie", title: "Bradford Ghosts" },
    { type: "collection", title: "The Brady Bunch Collection" },
    { type: "series", title: "Breaking Bad" },
  ];
  assert.deepEqual(titles(rankByMatch(items, "brad")), [
    "Bradford Ghosts",
    "The Brady Bunch Collection",
    "Breaking Bad",
    "Brad Pitt",
    "Bradley Cooper",
  ]);
});

test("rankByMatch keeps popularity order within a level and nudges owned titles", () => {
  const items = [
    { type: "movie", title: "Alien: Romulus" },
    { type: "movie", title: "Aliens" },
    { type: "movie", title: "Alien Resurrection", in_library: true },
  ];
  assert.deepEqual(titles(rankByMatch(items, "alien")), ["Alien Resurrection", "Alien: Romulus", "Aliens"]);
});

test("rankByMatch never lifts an owned title over a better match", () => {
  const items = [
    { type: "movie", title: "Predalien", watched: true },
    { type: "movie", title: "Alien" },
  ];
  assert.deepEqual(titles(rankByMatch(items, "alien")), ["Alien", "Predalien"]);
});

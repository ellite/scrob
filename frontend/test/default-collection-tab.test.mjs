import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { mediaNavHref } from "../src/lib/media-navigation.js";

test("navigation defaults to Explore without a saved preference", () => {
  assert.equal(mediaNavHref("movies"), "/movies");
  assert.equal(mediaNavHref("shows", false), "/shows");
});

test("the collection preference applies to both media navigation links", () => {
  assert.equal(mediaNavHref("movies", true), "/collection/movies");
  assert.equal(mediaNavHref("shows", true), "/collection/shows");
});

test("desktop and mobile navigation use the account preference", async () => {
  const source = await readFile(new URL("../src/layouts/Base.astro", import.meta.url), "utf8");
  assert.match(source, /!!user && Astro.locals.settings\?\.default_collection_tab === true/);
  for (const type of ["movies", "shows"]) {
    assert.equal(source.split(`data-media-nav="${type}" href={mediaNavHref("${type}", defaultCollectionTab)}`).length - 1, 2);
  }
});

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const component = await readFile(new URL("../src/components/MediaViewToggle.astro", import.meta.url), "utf8");
const mediaCard = await readFile(new URL("../src/components/MediaCard.astro", import.meta.url), "utf8");

test("compact media view is accessible, persistent, and defaults to grid", () => {
  assert.match(component, /aria-label="Use grid view"/);
  assert.match(component, /aria-label="Use compact list view"/);
  assert.match(component, /aria-pressed="true"/);
  assert.match(component, /const STORAGE_KEY = "scrob-media-view"/);
  assert.match(component, /localStorage\.getItem\(STORAGE_KEY\) === "list" \? "list" : "grid"/);
  assert.match(component, /localStorage\.setItem\(STORAGE_KEY, view\)/);
  assert.match(component, /target\.dataset\.mediaView = view/);
});

test("compact mode reflows existing cards instead of dropping card actions", () => {
  assert.match(component, /\[data-media-view-container\]\[data-media-view="list"\]/);
  assert.match(component, /\[data-card-actions\]/);
  assert.match(mediaCard, /<div data-card-actions>/);
  assert.match(mediaCard, /<CardActionBar/);
});

test("all Movies and Shows browse surfaces use the shared control and target", async () => {
  const pages = [
    ["../src/pages/movies.astro", "movie-results"],
    ["../src/pages/shows.astro", "show-results"],
    ["../src/pages/collection/movies.astro", "collection-movie-results"],
    ["../src/pages/collection/shows.astro", "collection-show-results"],
  ];

  for (const [file, targetId] of pages) {
    const source = await readFile(new URL(file, import.meta.url), "utf8");
    assert.match(source, /import MediaViewToggle/);
    assert.match(source, new RegExp(`<MediaViewToggle targetId="${targetId}"`));
    assert.match(source, new RegExp(`id="${targetId}" data-media-view-container data-media-view="grid"`));
  }
});

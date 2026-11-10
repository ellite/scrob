import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { resolvePageSize, pageSizeUrl } from "../src/lib/page-size.js";

test("explicit sizes override the default and invalid sizes fall back", () => {
  for (const size of [20, 30, 40, 50, 100]) {
    assert.equal(resolvePageSize(String(size), 30), size);
  }
  for (const invalid of [null, "", "0", "101", "20.5", "abc"]) {
    assert.equal(resolvePageSize(invalid, 50), 50);
  }
});

test("changing size resets pagination and preserves repeated filters", () => {
  const url = new URL(pageSizeUrl("https://scrob.example/collection/movies?page=7&genre=Drama&genre=Comedy&sort=title", 100));
  assert.equal(url.searchParams.has("page"), false);
  assert.equal(url.searchParams.get("page_size"), "100");
  assert.deepEqual(url.searchParams.getAll("genre"), ["Drama", "Comedy"]);
  assert.equal(url.searchParams.get("sort"), "title");
});

for (const page of ["collection/movies.astro", "collection/shows.astro", "history.astro"]) {
  test(`${page} sends and preserves the selected page size`, async () => {
    const source = await readFile(new URL(`../src/pages/${page}`, import.meta.url), "utf8");
    assert.match(source, /page_size: pageSize/);
    assert.match(source, /<PageSizeSelect value=\{pageSize\}/);
    assert.match(source, /page_size/);
  });
}

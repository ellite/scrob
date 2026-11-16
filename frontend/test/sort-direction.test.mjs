import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

import { defaultDir, dirArrow, effectiveDir, nextSort, parseDir } from "../src/lib/sortDirection.ts";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("defaults keep today's fixed orders", () => {
  assert.equal(defaultDir("title"), "asc");
  for (const sort of ["release_date", "rating", "added_at"]) assert.equal(defaultDir(sort), "desc", sort);
});

test("junk directions fall back to the sort's default", () => {
  assert.equal(parseDir("sideways"), undefined);
  assert.equal(effectiveDir("title", "desc"), "desc");
  assert.equal(effectiveDir("release_date", null), "desc");
});

test("picking the active sort again flips it, another sort starts at its default", () => {
  assert.deepEqual(nextSort("release_date", undefined, "release_date"), { sort: "release_date", dir: "asc" });
  assert.deepEqual(nextSort("release_date", "asc", "release_date"), { sort: "release_date", dir: "desc" });
  assert.deepEqual(nextSort("title", "asc", "title"), { sort: "title", dir: "desc" });
  assert.deepEqual(nextSort("release_date", "asc", "title"), { sort: "title", dir: "asc" });
});

test("arrows: down = descending, up = ascending", () => {
  assert.equal(dirArrow("desc"), "↓");
  assert.equal(dirArrow("asc"), "↑");
});

test("the list page opts into directions and sorts with them", async () => {
  const page = await readFile(path.join(frontendRoot, "src", "pages", "list", "[id].astro"), "utf8");
  assert.match(page, /id="list-items-sort"[\s\S]*?dir="desc"/);
  assert.match(page, /applyItemsSort\(e\.detail\.value, e\.detail\.dir\)/);
  assert.match(page, /effectiveDir\(sortKey, dir\) === 'desc'/);
});

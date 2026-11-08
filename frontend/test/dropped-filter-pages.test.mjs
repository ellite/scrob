import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function pageSource(page) {
  return readFile(path.join(frontendRoot, "src", "pages", page), "utf8");
}

for (const page of ["movies.astro", "shows.astro"]) {
  test(`${page} keeps the Dropped filter in the Explore request and pagination URL`, async () => {
    const source = await pageSource(page);
    assert.match(source, /getAll\("dropped"\)/);
    assert.match(source, /dropped:\s*dropped/);
    assert.match(source, /append\("dropped", value\)/);
    assert.match(source, /key:\s*"dropped", label:\s*m\.filter_dropped\(\)/);
    assert.match(source, /value:\s*"notdropped", label:\s*m\.filter_not_dropped_option\(\)/);
  });
}

test("the API client exposes Dropped to Explore requests", async () => {
  const source = await readFile(path.join(frontendRoot, "src", "lib", "api.ts"), "utf8");
  assert.match(source, /tmdbList:[\s\S]*dropped\?: string\[\]/);
});

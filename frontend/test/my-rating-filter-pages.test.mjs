import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function pageSource(page) {
  return readFile(path.join(frontendRoot, "src", "pages", page), "utf8");
}

for (const page of ["movies.astro", "shows.astro", "collection/movies.astro", "collection/shows.astro"]) {
  test(`${page} keeps the My Rating filter in the page request and pagination URL`, async () => {
    const source = await pageSource(page);

    assert.match(source, /import \{ MY_RATING_OPTIONS, parseMyRating \} from "(\.\.\/)+lib\/my-rating"/);
    assert.match(source, /const myRating = parseMyRating\(Astro\.url\)/);
    assert.match(source, /my_rating:\s*myRating/);
    assert.match(source, /append\("my_rating", String\(rating\)\)/);
    assert.match(source, /key:\s*"my_rating", label:\s*"My Rating"/);
  });
}

for (const page of ["collection/movies.astro", "collection/shows.astro"]) {
  test(`${page} offers descending My Rating as a collection sort`, async () => {
    const source = await pageSource(page);
    assert.match(source, /value:\s*"user_rating", label:\s*"My Rating"/);
  });
}

test("the My Rating values and chips are defined once, in lib/my-rating.ts", async () => {
  const source = await readFile(path.join(frontendRoot, "src", "lib", "my-rating.ts"), "utf8");
  assert.match(source, /getAll\("my_rating"\).*Number\.isInteger\(n\) && n >= 1 && n <= 10/);
  assert.match(source, /export const MY_RATING_OPTIONS/);
  for (const page of ["movies.astro", "shows.astro", "collection/movies.astro", "collection/shows.astro"]) {
    assert.doesNotMatch(await pageSource(page), /getAll\("my_rating"\)|const MY_RATING_OPTIONS/);
  }
});

test("the API client exposes My Rating to the movie, show, and explore requests", async () => {
  const source = await readFile(path.join(frontendRoot, "src", "lib", "api.ts"), "utf8");
  assert.equal((source.match(/my_rating\?: number\[\]/g) ?? []).length, 3);
});

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function source(...parts) {
  return readFile(path.join(frontendRoot, ...parts), "utf8");
}

test("person page requests grouped credits and offers a job filter that keeps the other filters", async () => {
  const page = await source("src", "pages", "person", "[id].astro");
  assert.match(page, /grouped:\s*true,\s*creditJob/);
  assert.match(page, /key:\s*"credit_job", label:\s*m\.person_filter_job\(\)/);
  assert.match(page, /<PersonCreditGroups groups=\{creditGroups\}/);
  // The library, genre, year and rating filters stay part of the request.
  assert.match(page, /\{ collection, genre, year, minRating, grouped: true, creditJob \}/);
});

test("credit groups load more per group through the partial with the active filters", async () => {
  const groups = await source("src", "components", "PersonCreditGroups.astro");
  assert.match(groups, /new URLSearchParams\(window\.location\.search\)/);
  assert.match(groups, /params\.set\("credit_kind", group\.dataset\.kind!\)/);
  assert.match(groups, /params\.set\("credit_job", group\.dataset\.job!\)/);
  assert.match(groups, /fetch\("\/partials\/person-credits\?"/);

  const partial = await source("src", "pages", "partials", "person-credits.astro");
  assert.match(partial, /searchParams\.get\("credit_kind"\)/);
  assert.match(partial, /searchParams\.get\("credit_job"\)/);
  assert.match(partial, /minRating, creditKind, creditJob/);
});

test("every person-page text and department name is localized in all languages", async () => {
  const keys = [
    "person_filter_job", "person_load_more", "person_department_acting", "person_department_directing",
    "person_department_writing", "person_department_camera", "person_department_editing",
    "person_department_music", "person_department_visual_effects", "person_department_other",
    "person_department_thanks", "person_department_production",
  ];
  for (const lang of ["en", "fr", "pt-PT"]) {
    const messages = JSON.parse(await source("messages", `${lang}.json`));
    for (const key of keys) assert.ok(messages[key], `${lang} is missing ${key}`);
  }
  const labels = await source("src", "lib", "labels.ts");
  for (const key of keys.filter((k) => k.startsWith("person_department_"))) {
    assert.match(labels, new RegExp(`m\\.${key}\\b`));
  }
});

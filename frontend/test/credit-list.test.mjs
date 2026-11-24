import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";
import { creditSearchText, matchesCreditQuery } from "../src/lib/creditList.ts";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("crew search matches name or job, ignores case and accents, and needs every word", () => {
  const row = creditSearchText("Alfonso Cuarón", "Director, Screenplay");
  assert.equal(matchesCreditQuery(row, ""), true);
  assert.equal(matchesCreditQuery(row, "  "), true);
  assert.equal(matchesCreditQuery(row, "cuaron"), true);
  assert.equal(matchesCreditQuery(row, "SCREENPLAY"), true);
  assert.equal(matchesCreditQuery(row, "alfonso director"), true);
  assert.equal(matchesCreditQuery(row, "alfonso gaffer"), false);
  assert.equal(matchesCreditQuery(row, "composer"), false);
});

test("search text skips missing parts instead of writing 'undefined'", () => {
  assert.equal(creditSearchText("Ana Silva", null, undefined), "ana silva");
  assert.equal(matchesCreditQuery(creditSearchText("Ana Silva", undefined), "undefined"), false);
});

test("movie and show pages pass the full crew to the cast/crew section", async () => {
  const movie = await readFile(path.join(frontendRoot, "src", "pages", "media", "[type]", "[id].astro"), "utf8");
  const show = await readFile(path.join(frontendRoot, "src", "pages", "show", "[id].astro"), "utf8");
  assert.match(movie, /<CastCrewSection[^>]*fullCrew=\{item\.full_crew\}/);
  assert.match(show, /<CastCrewSection[^>]*fullCrew=\{show\.full_crew\}/);
});

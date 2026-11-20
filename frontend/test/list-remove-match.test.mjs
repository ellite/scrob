import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// #586: a movie and a show on one list can share a TMDB id (TMDB numbers them
// separately). Unticking the movie must never delete the show's row.
test("list popover removal never matches a list row of a different media type", async () => {
  const source = await readFile(path.join(frontendRoot, "src", "layouts", "Base.astro"), "utf8");
  const start = source.indexOf("const wantType");
  assert.ok(start > 0, "removal branch computes the wanted media type");
  const block = source.slice(start, start + 900);
  assert.match(block, /_listMediaType === 'show' \? 'series' : _listMediaType/);
  assert.match(block, /i\.media\.type !== wantType\) return false/);
  // the type guard must run before any id comparison
  assert.ok(block.indexOf("i.media.type !== wantType") < block.indexOf("const sameItem"));
});

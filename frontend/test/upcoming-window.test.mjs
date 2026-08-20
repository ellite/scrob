import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const page = await readFile(new URL("../src/pages/index.astro", import.meta.url), "utf8");

test("homepage has an accessible persisted Today/7 days chooser", () => {
  assert.match(page, /role="group" aria-label="Airing episode date range"/);
  assert.match(page, /data-airing-window="1"/);
  assert.match(page, /data-airing-window="7"/);
  assert.match(page, /localStorage\.getItem\(airingPreferenceKey\)/);
  assert.match(page, /localStorage\.setItem\(airingPreferenceKey/);
});

test("seven-day requests pass the browser timezone and label only calendar dates", () => {
  assert.match(page, /Intl\.DateTimeFormat\(\)\.resolvedOptions\(\)\.timeZone/);
  assert.match(page, /airing-today\/collected\?timezone=/);
  assert.match(page, /&days=\$\{days\}/);
  assert.match(page, /no airtimes are implied/);
  assert.match(page, /episodeDateLabel\(item\.release_date\)/);
});

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pageSource = () => readFile(path.join(frontendRoot, "src", "pages", "diagnostics.astro"), "utf8");

// The page rebuilds every job card on each 5 s poll; an expanded warnings list
// must stay open across that rebuild instead of snapping shut.
test("diagnostics keeps an expanded warnings list open across a refresh", async () => {
  const src = await pageSource();
  assert.match(src, /querySelectorAll<HTMLDetailsElement>\("details\[open\]\[data-job-id\]"\)/);
  assert.match(src, /details\.dataset\.jobId = String\(job\.id\)/);
  assert.match(src, /details\.open = openJobIds\.has\(String\(job\.id\)\)/);
  // Collected before the rebuild: afterwards the old open lists are gone.
  assert.ok(src.indexOf("const openJobIds = new Set(") < src.indexOf("list.replaceChildren("));
});

// Opening one warnings list closes the others. "toggle" doesn't bubble, so the
// list listens in the capture phase.
test("diagnostics opens only one warnings list at a time", async () => {
  const src = await pageSource();
  assert.match(src, /list\.addEventListener\("toggle", [\s\S]*?, true\)/);
  assert.match(src, /other !== opened\) other\.open = false/);
});

// Every message the page and the warning lib use exists in all three locales
// and keeps the same {placeholders} (a renamed one would print "undefined").
test("diagnostics messages exist in all locales with the same placeholders", async () => {
  const page = await pageSource();
  const lib = await readFile(path.join(frontendRoot, "src", "lib", "syncWarnings.ts"), "utf8");
  const keys = new Set([
    ...[...page.matchAll(/\bm\.([a-z0-9_]+)/g)].map((match) => match[1]),
    ...[...lib.matchAll(/^ {2}((?:diag|conn)_[a-z_]+): Message/gm)].map((match) => match[1]),
  ]);
  assert.ok(keys.size > 30);
  const placeholders = (value) => [...new Set([...JSON.stringify(value).matchAll(/\{(\w+)\}/g)].map((match) => match[1]))].sort();
  const locales = {};
  for (const locale of ["en", "fr", "pt-PT"]) {
    locales[locale] = JSON.parse(await readFile(path.join(frontendRoot, "messages", `${locale}.json`), "utf8"));
  }
  for (const key of keys) {
    for (const locale of Object.keys(locales)) assert.ok(key in locales[locale], `${locale}.json is missing ${key}`);
    for (const locale of ["fr", "pt-PT"]) {
      assert.deepEqual(placeholders(locales[locale][key]), placeholders(locales.en[key]), `${locale}.json ${key} placeholders differ`);
    }
  }
});

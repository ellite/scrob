import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

import { describeSyncWarnings } from "../src/lib/syncWarnings.ts";

// Build the message functions from the real en.json (the Paraglide output only
// exists after a build), so a missing key fails here and not in the browser.
const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const en = JSON.parse(await readFile(path.join(frontendRoot, "messages", "en.json"), "utf8"));

const fill = (text, params) => text.replace(/\{(\w+)\}/g, (_, name) => String(params?.[name]));
const t = new Proxy({}, {
  get(_, key) {
    const message = en[key];
    assert.ok(message !== undefined, `missing message key ${String(key)}`);
    return (params) => {
      if (typeof message === "string") return fill(message, params);
      const match = message[0].match;
      return fill(params.count === 1 ? match["countPlural=one"] : match["countPlural=other"], params);
    };
  },
});

const describe = (warnings) => describeSyncWarnings(warnings, t);

test("a missing TMDB season reads as one line", () => {
  assert.deepEqual(
    describe([{ show: "The Killing", tmdb_id: 46648, season: 4, affected_episodes: 6, reason: "Season not found on TMDB" }]),
    ["The Killing, season 4: not on TMDB (6 episodes)"],
  );
});

test("unmatched titles name their type and reason", () => {
  assert.deepEqual(
    describe([{ title: "Bernd Channel", media_type: "series", source_id: "1", reason: "Unmatched on source" }]),
    ["Bernd Channel (series): Unmatched on source"],
  );
});

test("a matched title says what it was matched to", () => {
  assert.deepEqual(
    describe([
      { title: "Bernd Channel", media_type: "series", reason: "Unmatched", matched: true, matched_tvdb_id: 383356, matched_show_title: "Bernd das Brot" },
      { title: "Other", media_type: "series", reason: "Unmatched", matched: true, matched_tvdb_id: 1 },
      { title: "Third", media_type: "series", reason: "Unmatched", matched: true },
      { title: "Fourth", reason: "Unmatched", matched: true, matched_show_title: "Fourth Show" },
    ]),
    [
      "Bernd Channel (series): matched to Bernd das Brot (TVDB 383356)",
      "Other (series): matched via TVDB 1",
      "Third (series): matched",
      "Fourth: matched to Fourth Show",
    ],
  );
});

test("push warnings are grouped per series", () => {
  const ep = { type: "watched_lookup_failed", media_type: "episode", series_name: "Lost", title: "Ep" };
  assert.deepEqual(
    describe([
      ep, ep, ep,
      { type: "watched_lookup_failed", media_type: "movie", title: "Heat" },
      { type: "push_failed", media_type: "movie", title: "Alien", action: "rating", reason: "HTTP 500" },
    ]),
    ["Lost: 3 episodes not on this server", "Heat (movie): not on this server", "Alien (movie) · rating: HTTP 500"],
  );
});

test("failed episode pushes are grouped by series, action and reason", () => {
  const ep = (action, reason) => ({ type: "push_failed", media_type: "episode", series_name: "Dark", title: "Ep", action, reason });
  assert.deepEqual(
    describe([ep("watched", "HTTP 500"), ep("watched", "HTTP 500"), ep("season_rating", "HTTP 400"), ep(undefined, "boom")]),
    ["Dark: 2 episodes · watched: HTTP 500", "Dark: 1 episode · season rating: HTTP 400", "Dark: 1 episode · boom"],
  );
});

test("a failed push without a reason says it failed", () => {
  assert.deepEqual(describe([{ type: "push_failed", media_type: "movie", title: "Alien" }]), ["Alien (movie): failed"]);
  assert.deepEqual(describe([{ type: "push_failed", media_type: "movie" }]), ["Untitled (movie): failed"]);
});

test("episodes of unmatched series are grouped too", () => {
  const ep = { title: "Ep", media_type: "episode", series_name: "Bluey", reason: "Unmatched on source" };
  assert.deepEqual(describe([ep, ep]), ["Bluey: 2 episodes · Unmatched on source"]);
});

test("strings pass through and unknown shapes stay JSON", () => {
  assert.deepEqual(describe(["plain text", { foo: 1 }, null]), ["plain text", '{"foo":1}', "null"]);
});

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// #578: the media-server "Watched status" pull flag also gates live webhook
// scrobbling, so the card has to say so.
test("media-server connection cards explain that Watched status also gates webhook scrobbling", async () => {
  const page = await readFile(path.join(frontendRoot, "src", "pages", "connections.astro"), "utf8");
  assert.match(page, /conn\.type === "jellyfin" \|\| conn\.type === "emby" \|\| conn\.type === "plex"\) && \(\s*<p[^>]*>\{m\.conn_sync_watched_hint\(\)\}<\/p>/);
  for (const lang of ["en", "fr", "pt-PT"]) {
    const messages = JSON.parse(await readFile(path.join(frontendRoot, "messages", `${lang}.json`), "utf8"));
    assert.ok(messages.conn_sync_watched_hint?.length > 20, `${lang} has the hint`);
  }
});

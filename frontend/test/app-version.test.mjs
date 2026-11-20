import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { readAppVersion } from "../src/lib/appVersion.ts";

async function withFile(content, fn) {
  const dir = await mkdtemp(path.join(tmpdir(), "scrob-version-"));
  const file = path.join(dir, "VERSION");
  try {
    if (content !== null) await writeFile(file, content);
    return await fn(file);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

test("the baked version file wins over a stale APP_VERSION env var (#140)", async () => {
  await withFile("2.40.0\n", (file) => {
    assert.equal(readAppVersion({ APP_VERSION: "1.52.1" }, file), "2.40.0");
  });
});

test("falls back to APP_VERSION when there is no version file", async () => {
  await withFile(null, (file) => {
    assert.equal(readAppVersion({ APP_VERSION: "2.1.0" }, file), "2.1.0");
  });
});

test("an empty version file falls back too", async () => {
  await withFile("  \n", (file) => {
    assert.equal(readAppVersion({ APP_VERSION: "2.1.0" }, file), "2.1.0");
  });
});

test("defaults to dev when nothing is set", async () => {
  await withFile(null, (file) => {
    assert.equal(readAppVersion({}, file), "dev");
  });
});

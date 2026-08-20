import assert from "node:assert/strict";
import test from "node:test";

import { readImageVersion } from "../src/lib/imageVersion.mjs";

test("uses the trimmed version baked into the image", async () => {
  assert.equal(
    await readImageVersion(async () => "1.53.1\n", { APP_VERSION: "1.52.1" }),
    "1.53.1",
  );
});

test("falls back to the environment outside an image", async () => {
  assert.equal(
    await readImageVersion(async () => { throw new Error("missing"); }, { APP_VERSION: "2.1.0" }),
    "2.1.0",
  );
});

test("uses dev when neither source has a version", async () => {
  assert.equal(await readImageVersion(async () => "   \n", {}), "dev");
  assert.equal(await readImageVersion(async () => { throw new Error("missing"); }, {}), "dev");
});

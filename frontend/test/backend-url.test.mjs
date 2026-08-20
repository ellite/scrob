import assert from "node:assert/strict";
import test from "node:test";

import { resolveBackendUrl } from "../src/lib/backend-url.mjs";

test("uses the loopback default when no backend environment is configured", () => {
  assert.equal(resolveBackendUrl({}), "http://127.0.0.1:7331");
});

test("uses BACKEND_PORT at runtime for a loopback backend", () => {
  assert.equal(resolveBackendUrl({ BACKEND_PORT: "8123" }), "http://127.0.0.1:8123");
});

test("BACKEND_URL overrides BACKEND_PORT for a separately hosted backend", () => {
  assert.equal(
    resolveBackendUrl({ BACKEND_URL: "https://api.scrob.example", BACKEND_PORT: "8123" }),
    "https://api.scrob.example"
  );
});

for (const environment of [
  { BACKEND_PORT: "0" },
  { BACKEND_PORT: "7331.5" },
  { BACKEND_PORT: "not-a-port" },
  { BACKEND_URL: "api.scrob.example" },
  { BACKEND_URL: "ftp://api.scrob.example" },
  { BACKEND_URL: "https://api.scrob.example/v1" },
  { BACKEND_URL: "https://user:secret@api.scrob.example" },
]) {
  test(`rejects invalid backend configuration ${JSON.stringify(environment)}`, () => {
    assert.throws(() => resolveBackendUrl(environment));
  });
}

import assert from "node:assert/strict";
import test from "node:test";

import { publicOrigin } from "../src/lib/publicOrigin.ts";

const req = (headers) => new Request("http://localhost:7330/list/1", { headers });
const url = new URL("http://localhost:7330/list/1");

test("uses the Host header instead of Astro's localhost fallback (#534)", () => {
  assert.equal(publicOrigin(url, req({ host: "192.168.68.64:7330" })), "http://192.168.68.64:7330");
});

test("prefers the reverse proxy's forwarded host and scheme", () => {
  const r = req({ host: "scrob:7330", "x-forwarded-host": "scrob.example.com, internal", "x-forwarded-proto": "https" });
  assert.equal(publicOrigin(url, r), "https://scrob.example.com");
});

test("appends a forwarded port only when the host has none", () => {
  assert.equal(publicOrigin(url, req({ "x-forwarded-host": "nas.lan", "x-forwarded-port": "8443" })), "http://nas.lan:8443");
  assert.equal(publicOrigin(url, req({ "x-forwarded-host": "nas.lan:9000", "x-forwarded-port": "8443" })), "http://nas.lan:9000");
});

test("supports bracketed IPv6 hosts", () => {
  assert.equal(publicOrigin(url, req({ host: "[fd00::1]:7330" })), "http://[fd00::1]:7330");
});

test("falls back to the request origin for a missing or malformed host", () => {
  assert.equal(publicOrigin(url, req({})), "http://localhost:7330");
  assert.equal(publicOrigin(url, req({ host: "evil.com/<script>" })), "http://localhost:7330");
});

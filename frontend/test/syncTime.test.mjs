import assert from "node:assert/strict";
import test from "node:test";

import { parseServerTime } from "../src/lib/syncTime.ts";

test("a timestamp without an offset is read as UTC", () => {
  assert.equal(parseServerTime("2026-10-19T12:00:00").getTime(), Date.UTC(2026, 9, 19, 12));
});

test("a UTC timestamp is not shifted again", () => {
  assert.equal(parseServerTime("2026-10-19T12:00:00Z").getTime(), Date.UTC(2026, 9, 19, 12));
});

test("an explicit offset is kept", () => {
  assert.equal(parseServerTime("2026-10-19T12:00:00+02:00").getTime(), Date.UTC(2026, 9, 19, 10));
});

test("Python microseconds parse to milliseconds", () => {
  assert.equal(parseServerTime("2026-10-19T12:00:00.123456").getTime(), Date.UTC(2026, 9, 19, 12, 0, 0, 123));
});

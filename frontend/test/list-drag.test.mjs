import assert from "node:assert/strict";
import test from "node:test";
import { dropBefore } from "../src/lib/list-order.js";

test("drop uses the nearest edge of the target row", () => {
  const target = { nextElementSibling: null };
  assert.equal(dropBefore(target, 10, { top: 0, height: 40 }), target);
  assert.equal(dropBefore(target, 30, { top: 0, height: 40 }), null);
});

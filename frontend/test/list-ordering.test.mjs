import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { reconcileOrder, initialItemSort } from "../src/lib/list-order.js";

test("lists without a manual order keep Recently Added as their default", () => {
  assert.equal(initialItemSort([{ sort_order: 0 }, { sort_order: 0 }]), "added_at");
  assert.equal(initialItemSort([{ sort_order: -2 }, { sort_order: -1 }]), "saved_order");
});

test("adding and removing entries preserves pending moves and removes stale IDs", () => {
  assert.deepEqual(reconcileOrder([3, 1, 2], [1, 3, 4]), [3, 1, 4]);
  assert.deepEqual(reconcileOrder([], [1, 2]), [1, 2]);
  assert.deepEqual(reconcileOrder([1, 2], []), []);
});

test("list ordering is owner-only and keeps the watchlist pinned", async () => {
  const source = await readFile(new URL("../src/pages/lists.astro", import.meta.url), "utf8");
  assert.match(source, /user && <ReorderList/);
  assert.match(source, /lists\.filter\(list => !list\.is_watchlist\)/);
  assert.match(source, /endpoint="\/api\/proxy\/lists\/order"/);
});

test("saved item order survives initial client-side sorting", async () => {
  const source = await readFile(new URL("../src/pages/list/[id].astro", import.meta.url), "utf8");
  assert.match(source, /list\.is_owner && <ReorderList/);
  assert.match(source, /current=\{initialItemSort\(list.items\)\}/);
  assert.match(source, /data-saved-order=\{index\}/);
  assert.match(source, /applyItemsSort\(document.getElementById\('list-page'\)\?\.dataset.initialSort/);
  assert.doesNotMatch(source, /applyItemsSort\('added_at'\)/);
});

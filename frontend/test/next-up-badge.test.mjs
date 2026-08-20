import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const root = new URL("..", import.meta.url);
const read = (path) => readFile(new URL(path, root), "utf8");

test("Next Up badges have one accessible label and distinct status colours", async () => {
  const component = await read("src/components/NextUpBadge.astro");
  const statuses = await read("src/lib/next-up-status.ts");

  for (const label of ["Season Finale", "Season Premiere", "New Today", "Next Episode"]) {
    assert.match(statuses, new RegExp(`label: \\"${label}\\"`));
  }
  assert.match(component, /uppercase tracking-wider/);
  assert.match(component, /variant === "overlay" \? "absolute top-2 right-2 z-10"/);
});

test("all Next Up surfaces pass the backend-derived status to the shared badge", async () => {
  const index = await read("src/pages/index.astro");
  const episodeCard = await read("src/components/EpisodeCard.astro");
  const mediaCard = await read("src/components/MediaCard.astro");
  const fullNextUp = await read("src/pages/next-up.astro");

  assert.match(index, /EpisodeCard item=\{item\} nextUpStatus=\{item\.next_up_status\}/);
  assert.match(index, /NextUpBadge status=\{hero\.next_up_status\} variant="inline"/);
  assert.match(index, /MediaCard item=\{item\} nextUpStatus=\{item\.next_up_status\}/);
  assert.match(episodeCard, /NextUpBadge status=\{nextUpStatus\}/);
  const cardStart = mediaCard.indexOf("{href ? (");
  const noHrefStart = mediaCard.indexOf("\n  ) : (", cardStart);
  const actionBarStart = mediaCard.indexOf("<!-- Action bar", noHrefStart);
  const badgeCount = (branch) => (branch.match(/<NextUpBadge status=\{nextUpStatus\} \/>/g) ?? []).length;
  assert.equal(badgeCount(mediaCard.slice(cardStart, noHrefStart)), 1);
  assert.equal(badgeCount(mediaCard.slice(noHrefStart, actionBarStart)), 1);
  assert.match(fullNextUp, /next_up_status/);
});

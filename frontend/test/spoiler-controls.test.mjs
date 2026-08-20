import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const base = readFileSync(new URL('../src/layouts/Base.astro', import.meta.url), 'utf8');
const card = readFileSync(new URL('../src/components/MediaCard.astro', import.meta.url), 'utf8');
const episodeCard = readFileSync(new URL('../src/components/EpisodeCard.astro', import.meta.url), 'utf8');
const nextUp = readFileSync(new URL('../src/pages/next-up.astro', import.meta.url), 'utf8');
const home = readFileSync(new URL('../src/pages/index.astro', import.meta.url), 'utf8');

test('spoiler controls are local, fail-closed before body render, and support keyboard reveal', () => {
  assert.match(base, /localStorage\.getItem\('spoilerControls'\)/);
  assert.match(base, /document\.documentElement\.setAttribute\('data-spoilers'/);
  assert.match(base, /event\.key === 'Enter' \|\| event\.key === ' '/);
  assert.match(base, /data-spoiler-revealed/);
  assert.match(base, /spoiler-content-added/);
  assert.match(base, /event\.key === 'Escape'/);
  assert.match(base, /parsed && typeof parsed === 'object' && !Array\.isArray\(parsed\)/);
  assert.match(base, /try \{ localStorage\.setItem\(SPOILER_KEY/);
  assert.match(base, /if \(wasOpen\) spoilerBtn\?\.focus\(\)/);
});

test('shared cards expose independently controllable ratings and opt-in next episode content', () => {
  assert.match(card, /data-spoiler="ratings"/);
  assert.match(card, /spoilerNext = false/);
  assert.match(episodeCard, /spoilerNext = false/);
  assert.match(episodeCard, /data-spoiler=\{spoilerNext \? "next"/);
  assert.match(nextUp, /data-spoiler="next"/);
  assert.match(nextUp, /data-spoiler="ratings"/);
  assert.match(home, /<div data-spoiler="next" class="absolute top-0/);
  const nextUpHero = home.slice(home.indexOf('const hero = nextUp[0]'));
  assert.match(nextUpHero, /<div data-spoiler="next" class="absolute top-0/);
  assert.doesNotMatch(nextUpHero, /overflow-hidden pointer-events-none/);
});

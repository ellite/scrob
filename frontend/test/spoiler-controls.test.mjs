import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const base = readFileSync(new URL('../src/layouts/Base.astro', import.meta.url), 'utf8');
const card = readFileSync(new URL('../src/components/MediaCard.astro', import.meta.url), 'utf8');
const episodeCard = readFileSync(new URL('../src/components/EpisodeCard.astro', import.meta.url), 'utf8');
const nextUp = readFileSync(new URL('../src/pages/next-up.astro', import.meta.url), 'utf8');
const home = readFileSync(new URL('../src/pages/index.astro', import.meta.url), 'utf8');
const mediaDetail = readFileSync(new URL('../src/pages/media/[type]/[id].astro', import.meta.url), 'utf8');
const comments = readFileSync(new URL('../src/components/CommentsSection.astro', import.meta.url), 'utf8');
const detailRoutes = {
  tmdbShow: readFileSync(new URL('../src/pages/show/[id].astro', import.meta.url), 'utf8'),
  tvdbShow: readFileSync(new URL('../src/pages/show/tvdb/[id].astro', import.meta.url), 'utf8'),
  tmdbSeason: readFileSync(new URL('../src/pages/show/[id]/season/[season_number].astro', import.meta.url), 'utf8'),
  tvdbSeason: readFileSync(new URL('../src/pages/show/tvdb/[id]/season/[season_number].astro', import.meta.url), 'utf8'),
  tmdbEpisode: readFileSync(new URL('../src/pages/show/[id]/season/[season_number]/[episode_number].astro', import.meta.url), 'utf8'),
  tvdbEpisode: readFileSync(new URL('../src/pages/show/tvdb/[id]/season/[season_number]/[episode_number].astro', import.meta.url), 'utf8'),
};

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
  assert.match(base, /node\.closest\('a, button'\)/);
  assert.match(base, /const nativeControl = node\.matches/);
  assert.match(base, /interactive\.querySelectorAll/);
  assert.match(base, /node\.dataset\.spoilerRevealed !== 'true'/);
  assert.doesNotMatch(base, /node\.tabIndex = enabled/);
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

test('all TMDB and TVDB detail route families mark every displayed spoiler category', () => {
  assert.match(mediaDetail, /data-spoiler="ratings"/);
  assert.match(mediaDetail, /data-spoiler="descriptions"/);
  assert.match(mediaDetail, /data-spoiler="runtime"/);

  for (const route of [detailRoutes.tmdbShow, detailRoutes.tvdbShow]) {
    assert.match(route, /data-spoiler="descriptions"/);
    assert.match(route, /data-spoiler="ratings"/);
  }
  for (const route of [detailRoutes.tmdbSeason, detailRoutes.tvdbSeason]) {
    assert.match(route, /data-spoiler="ratings"/);
    assert.match(route, /data-spoiler="descriptions"/);
    assert.match(route, /data-spoiler="runtime"/);
  }
  for (const route of [detailRoutes.tmdbEpisode, detailRoutes.tvdbEpisode]) {
    assert.match(route, /data-spoiler="descriptions"/);
    assert.match(route, /data-spoiler="runtime"/);
  }
  assert.match(detailRoutes.tmdbEpisode, /data-spoiler="ratings"/);
});

test('comments keep their existing per-comment spoiler contract', () => {
  assert.match(comments, /comment\.is_spoiler/);
  assert.match(comments, /global media controls do not hide unflagged discussion/);
  assert.match(comments, /spoiler-overlay/);
  assert.match(comments, /blur-sm select-none pointer-events-none/);
});

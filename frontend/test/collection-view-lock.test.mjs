import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('..', import.meta.url);

async function source(path) {
  return readFile(new URL(path, root), 'utf8');
}

test('collection browsing starts locked and only marks collection action buttons as mutable', async () => {
  const [lock, cardActions, movies, shows] = await Promise.all([
    source('src/components/CollectionViewLock.astro'),
    source('src/components/CardActionBar.astro'),
    source('src/pages/collection/movies.astro'),
    source('src/pages/collection/shows.astro'),
  ]);

  assert.match(lock, /localStorage\.getItem\(storageKey\) !== 'false'/);
  assert.match(lock, /\[data-collection-mutation\]/);
  assert.match(lock, /button\.disabled = locked/);
  assert.match(lock, /aria-pressed/);
  assert.match(lock, /data-collection-lock-closed-icon/);
  assert.match(lock, /data-collection-lock-open-icon/);
  assert.match(lock, /closedIcon\?\.classList\.toggle\('hidden', !locked\)/);
  assert.match(lock, /openIcon\?\.classList\.toggle\('hidden', locked\)/);
  assert.match(cardActions, /data-collection-mutation=\{collectionReadOnly/);
  assert.match(cardActions, /disabled=\{collectionReadOnly\}/);
  assert.match(movies, /<CollectionViewLock\s*\/>/);
  assert.match(movies, /collectionReadOnly/);
  assert.match(shows, /<CollectionViewLock\s*\/>/);
  assert.match(shows, /collectionReadOnly/);
});

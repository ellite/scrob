import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { selectableSonarrSeasons, selectedSonarrSeasons } from '../src/lib/sonarrSeasons.ts';

const base = await readFile(new URL('../src/layouts/Base.astro', import.meta.url), 'utf8');

test('Sonarr request details expose an all-or-selected season choice', () => {
  assert.match(base, /id="customize-add-seasons-all" checked/);
  assert.match(base, /id="customize-add-seasons-selected"/);
  assert.match(base, /Monitor all regular seasons/);
  assert.match(base, /Specials stay unmonitored unless you choose them below/);
  assert.match(base, /Choose seasons/);
  assert.match(base, /customize-add-season-checkbox/);
});

test('only valid Sonarr seasons can be presented for selection', () => {
  assert.deepEqual(
    selectableSonarrSeasons([{ season_number: 2 }, { season_number: 0 }, { season_number: 2 }, { season_number: -1 }, {}]),
    [0, 2],
  );
});

test('all seasons keeps the existing Sonarr default and selected mode requires a choice', () => {
  assert.equal(selectedSonarrSeasons(false, []), undefined);
  assert.deepEqual(selectedSonarrSeasons(true, [2, 0, 2]), [2, 0]);
  assert.throws(() => selectedSonarrSeasons(true, []), /Select at least one season/);
  assert.throws(() => selectedSonarrSeasons(true, [-1]), /non-negative/);
  assert.match(base, /overrides\.selected_seasons = selectedSonarrSeasons/);
});

test('Sonarr options are fetched for the title being requested', () => {
  assert.match(base, /customize-options\?tmdb_id=\$\{encodeURIComponent\(tmdbId\)\}/);
  assert.match(base, /openCustomizeAddPopover\(mediaType, tmdbId, performRequestAdd\)/);
});

test('a queued-search failure leaves the add succeeded and warns against retrying', () => {
  assert.match(base, /res\.status === 'added_search_failed'/);
  assert.match(base, /Do not submit this request again/);
  assert.match(base, /search those seasons manually in Sonarr/);
});

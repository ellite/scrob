import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const page = await readFile(new URL('../src/pages/connections.astro', import.meta.url), 'utf8');

test('Connections warnings expose reversible per-item ignore controls', () => {
  assert.match(page, /\/api\/proxy\/sync\/unmatched-ignores/);
  assert.match(page, /class="ignore-unmatched-btn/);
  assert.match(page, /data-source-ids=/);
  assert.match(page, /class="unignore-unmatched-btn/);
  assert.match(page, /Ignored unmatched items/);
});

test('Connections uses the latest sync result instead of resurrecting old ignored warnings', () => {
  assert.match(page, /const latestCompletedJob = jobs\.find/);
  assert.match(page, /const job = latestCompletedJob\?\.warnings\?\.length \? latestCompletedJob : undefined/);
});

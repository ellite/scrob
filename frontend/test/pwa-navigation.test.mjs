import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const layout = await readFile(new URL('../src/layouts/Base.astro', import.meta.url), 'utf8');

test('shows an accessible back control only for standalone PWA history', () => {
  assert.match(layout, /id="pwa-back"[\s\S]*?type="button"[\s\S]*?aria-label="Go back"/);
  assert.match(layout, /class="hidden md:hidden/);
  assert.match(layout, /window\.matchMedia\('\(display-mode: standalone\)'\)\.matches/);
  assert.match(layout, /navigator as Navigator & \{ standalone\?: boolean \}/);
  assert.match(layout, /isStandalonePwa && window\.history\.length > 1/);
  assert.match(layout, /pwaBackButton\.addEventListener\('click', \(\) => window\.history\.back\(\)\)/);
});

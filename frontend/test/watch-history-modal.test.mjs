import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../src/layouts/Base.astro', import.meta.url), 'utf8');
const logHandler = source.slice(
  source.indexOf("document.getElementById('watch-history-log-btn')!.addEventListener('click'"),
  source.indexOf('// ── Main event delegation')
);

test('manual watch treats the POST as the failure boundary and closes after a best-effort refresh', () => {
  const post = logHandler.indexOf("await apiFetch('/history', 'POST', watchBody);");
  const postFailure = logHandler.indexOf('} catch (err) {', post);
  const localUpdate = logHandler.indexOf('updateWatchHistoryItemUI(true);', postFailure);
  const refreshTry = logHandler.indexOf('try {', localUpdate);
  const refresh = logHandler.indexOf('await loadWatchHistoryEvents();', refreshTry);
  const refreshFailure = logHandler.indexOf('} catch {', refresh);
  const close = logHandler.indexOf('closeWatchHistoryModal();', refreshFailure);

  assert.ok(post >= 0, 'manual watch submits history');
  assert.ok(postFailure > post, 'only POST failures enter the visible error path');
  assert.match(logHandler.slice(postFailure, localUpdate), /alertFromError\(err, 'Failed to mark as watched'\)/);
  assert.ok(localUpdate > postFailure, 'a successful POST updates card state before refresh');
  assert.ok(refresh > localUpdate, 'history refresh follows the durable write');
  assert.ok(refreshFailure > refresh, 'refresh errors are handled separately from POST errors');
  assert.ok(close > refreshFailure, 'the modal closes even when the follow-up refresh fails');
});

test('bulk season and show watches retain the same success-only close behavior', () => {
  const bulkPath = logHandler.slice(
    logHandler.indexOf('if (_watchHistoryBulkMode)'),
    logHandler.indexOf('\n        try {', logHandler.indexOf('if (_watchHistoryBulkMode)'))
  );
  const close = bulkPath.indexOf('closeWatchHistoryModal();');
  const failurePath = bulkPath.slice(bulkPath.indexOf('} catch (err)'));

  assert.ok(close >= 0, 'bulk submissions close after their API request and UI cascade');
  assert.equal(failurePath.includes('closeWatchHistoryModal();'), false, 'failed bulk submissions remain open');
});

test('every watch-history entry point resets the date picker to Just now', () => {
  const singleOpen = source.slice(source.indexOf('async function openWatchHistoryModal'), source.indexOf('function openBulkWatchModal'));
  const bulkOpen = source.slice(source.indexOf('function openBulkWatchModal'), source.indexOf('function cascadeWatchedUI'));

  assert.match(singleOpen, /resetWatchHistoryDateUI\(\)/);
  assert.match(bulkOpen, /resetWatchHistoryDateUI\(\)/);
  assert.match(source, /function resetWatchHistoryDateUI\(\)\s*\{[\s\S]*?_watchHistoryDateMode = 'now'/);
});

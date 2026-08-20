import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = async (path) => readFile(new URL(path, import.meta.url), 'utf8');

test('all individual history entry deletion controls require confirmation', async () => {
  const [base, historyCard] = await Promise.all([
    source('../src/layouts/Base.astro'),
    source('../src/components/HistoryCard.astro'),
  ]);
  const handlerStart = base.indexOf("document.getElementById('watch-history-modal-events')!.addEventListener");
  const modalRemoval = base.slice(
    handlerStart,
    base.indexOf("document.getElementById('watch-history-unwatch-btn')!.addEventListener", handlerStart)
  );
  const cardRemoval = historyCard.slice(
    historyCard.indexOf("btn.addEventListener('click'"),
    historyCard.indexOf('  });\n</script>')
  );

  for (const handler of [modalRemoval, cardRemoval]) {
    assert.match(handler, /showConfirm\(\s*'Remove history entry\?'/);
    assert.ok(handler.indexOf('if (!confirmed) return;') < handler.indexOf("'DELETE'"));
  }
});

test('all individual list-item deletion controls require confirmation', async () => {
  const [base, listPage] = await Promise.all([
    source('../src/layouts/Base.astro'),
    source('../src/pages/list/[id].astro'),
  ]);
  const popoverStart = base.indexOf('openListPopover');
  const popoverRemoval = base.slice(
    base.indexOf('            } else {', popoverStart),
    base.indexOf('// Update button state', popoverStart)
  );
  const detailRemoval = listPage.slice(
    listPage.indexOf('function bindRemoveButtons()'),
    listPage.indexOf('  // --- Search & Add ---')
  );

  for (const handler of [popoverRemoval, detailRemoval]) {
    assert.match(handler, /showConfirm\(\s*'Remove item from list\?'/);
    assert.ok(handler.indexOf('if (!confirmed) return;') < handler.indexOf("'DELETE'"));
  }
  assert.match(popoverRemoval, /if \(!confirmed\) \{\s*cb\.checked = true;\s*return;/);
});

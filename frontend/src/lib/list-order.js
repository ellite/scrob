export function reconcileOrder(current, available) {
  const existing = new Set(available);
  const kept = current.filter(id => existing.has(id));
  const saved = new Set(kept);
  return [...kept, ...available.filter(id => !saved.has(id))];
}

export function initialItemSort(items) {
  return items.some(item => item.sort_order < 0) ? "saved_order" : "added_at";
}

export function dropBefore(target, pointerY, bounds) {
  return pointerY < bounds.top + bounds.height / 2 ? target : target.nextElementSibling;
}

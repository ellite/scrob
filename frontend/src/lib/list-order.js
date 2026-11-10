export function reconcileOrder(current, available) {
  const existing = new Set(available);
  const kept = current.filter(id => existing.has(id));
  const saved = new Set(kept);
  return [...kept, ...available.filter(id => !saved.has(id))];
}

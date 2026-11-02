import { computeRange, setLoading } from "./format";
import { renderStats } from "./render";

const root = document.getElementById('stats-content') as HTMLElement;
const userId = root.dataset.userId as string;
const token = root.dataset.token || null;

// ── State ──────────────────────────────────────────────────────────────────
let currentPeriod = 'all';

let currentOffset = 0;

// ── Fetch ──────────────────────────────────────────────────────────────────
async function loadStats(since, until) {
  setLoading(true);
  try {
    const params = new URLSearchParams();
    if (since) params.set('since', since);
    if (until) params.set('until', until);
    const qs = params.toString();
    const res = await fetch(`/api/proxy/profile/${userId}/stats${qs ? '?' + qs : ''}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) { setLoading(false); return; }
    renderStats(await res.json(), since, until);
  } catch { /* silent */ }
  setLoading(false);
}

// ── Refresh ────────────────────────────────────────────────────────────────
function refresh() {
  const { since, until, label, canNav } = computeRange(currentPeriod, currentOffset);
  const nav = document.getElementById('period-nav');
  const periodLabel = document.getElementById('period-label');
  const nextBtn = document.getElementById('next-btn');
  const weekdayTitle = document.getElementById('weekday-chart-title');
  if (nav) nav.style.display = canNav ? 'flex' : 'none';
  if (periodLabel) periodLabel.textContent = label;
  if (nextBtn) nextBtn.disabled = currentOffset >= 0;
  if (weekdayTitle) {
    const isWeekly = currentPeriod === 'week' || currentPeriod === 'last7';
    weekdayTitle.textContent = isWeekly ? 'Activity by Day of Week' : 'Average Activity by Day of Week';
  }
  loadStats(since, until);
}

// ── Event listeners ────────────────────────────────────────────────────────
document.getElementById('period-select').addEventListener('change', e => {
  currentPeriod = e.target.value;
  currentOffset = 0;
  refresh();
});

document.getElementById('prev-btn').addEventListener('click', () => {
  currentOffset--;
  refresh();
});

document.getElementById('next-btn').addEventListener('click', () => {
  if (currentOffset < 0) { currentOffset++; refresh(); }
});

refresh();

// ── Period computation ─────────────────────────────────────────────────────
export const _fmt = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;

const _short = d => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

export function computeRange(period, offset) {
  if (period === 'all') return { since: null, until: null, label: 'All time', canNav: false };
  const today = new Date(); today.setHours(0, 0, 0, 0);
  let since, until, label;
  if (period === 'year') {
    const year = today.getFullYear() + offset;
    since = new Date(year, 0, 1); until = new Date(year, 11, 31);
    label = String(year);
  } else if (period === 'month') {
    const base = new Date(today.getFullYear(), today.getMonth() + offset, 1);
    since = base; until = new Date(base.getFullYear(), base.getMonth() + 1, 0);
    label = base.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
  } else if (period === 'week') {
    const dow = today.getDay();
    const monday = new Date(today);
    monday.setDate(today.getDate() + (dow === 0 ? -6 : 1 - dow) + offset * 7);
    const sunday = new Date(monday); sunday.setDate(monday.getDate() + 6);
    since = monday; until = sunday;
    label = monday.getMonth() === sunday.getMonth()
      ? `${monday.toLocaleDateString('en-US',{month:'short'})} ${monday.getDate()}–${sunday.getDate()}`
      : `${_short(monday)} – ${_short(sunday)}`;
  } else if (period === 'last365') {
    until = new Date(today); until.setDate(today.getDate() + offset * 365);
    since = new Date(until); since.setDate(until.getDate() - 364);
    label = offset === 0 ? 'Last 365 days' : `${_short(since)} – ${_short(until)}`;
  } else if (period === 'last30') {
    until = new Date(today); until.setDate(today.getDate() + offset * 30);
    since = new Date(until); since.setDate(until.getDate() - 29);
    label = offset === 0 ? 'Last 30 days' : `${_short(since)} – ${_short(until)}`;
  } else if (period === 'last7') {
    until = new Date(today); until.setDate(today.getDate() + offset * 7);
    since = new Date(until); since.setDate(until.getDate() - 6);
    label = offset === 0 ? 'Last 7 days' : `${_short(since)} – ${_short(until)}`;
  }
  return { since: _fmt(since), until: _fmt(until), label, canNav: true };
}

// ── Helpers ────────────────────────────────────────────────────────────────
export function formatMinutes(mins) {
  const h = Math.round(mins / 60);
  return h >= 48 ? `${(mins / 60 / 24).toFixed(1)}d` : `${h}h`;
}

export const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

const dayNames   = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

export function fillActivityGaps(activityData, since, until, isDaily) {
  if (!since || !until) return activityData;
  const dataMap = Object.fromEntries(activityData.map(d => [d.month, d]));
  const filled = [];
  if (isDaily) {
    const cur = new Date(since); cur.setHours(0,0,0,0);
    const end = new Date(until); end.setHours(0,0,0,0);
    while (cur <= end) {
      const key = _fmt(cur);
      filled.push(dataMap[key] || { month: key, movies: 0, episodes: 0 });
      cur.setDate(cur.getDate() + 1);
    }
  } else {
    const [sy, sm] = since.split('-').map(Number);
    const [uy, um] = until.split('-').map(Number);
    let cy = sy, cm = sm;
    while (cy < uy || (cy === uy && cm <= um)) {
      const key = `${cy}-${String(cm).padStart(2,'0')}`;
      filled.push(dataMap[key] || { month: key, movies: 0, episodes: 0 });
      if (++cm > 12) { cm = 1; cy++; }
    }
  }
  return filled;
}

export function getActivityLabel(d, isDaily, totalBars) {
  if (!isDaily) return monthNames[parseInt(d.month.slice(5), 10) - 1];
  const [y, m, day] = d.month.split('-').map(Number);
  if (totalBars <= 7) return dayNames[new Date(y, m-1, day).getDay()];
  return [1, 5, 10, 15, 20, 25].includes(day) ? String(day) : '';
}

// ── Loading state ──────────────────────────────────────────────────────────
export function setLoading(on) {
  const el = document.getElementById('stats-content');
  if (!el) return;
  el.style.opacity = on ? '0.4' : '';
  el.style.pointerEvents = on ? 'none' : '';
  el.style.transition = 'opacity 0.15s';
}

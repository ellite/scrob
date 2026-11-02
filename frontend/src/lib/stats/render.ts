import { _fmt, formatMinutes, monthNames, fillActivityGaps, getActivityLabel } from "./format";
import { renderBreakdownCharts } from "./charts";

// ── Render ─────────────────────────────────────────────────────────────────
export function renderStats(data, since, until) {
  const totalH = Math.round(data.total_watch_minutes / 60);
  const totalD = (data.total_watch_minutes / 60 / 24).toFixed(1);

  // Summary row
  const summaryRow = document.getElementById('summary-row');
  if (summaryRow) {
    summaryRow.innerHTML = [
      { value: data.movies_watched.toLocaleString(),   label: 'Movies Watched' },
      { value: data.shows_watched.toLocaleString(),    label: 'Shows Watched' },
      { value: data.episodes_watched.toLocaleString(), label: 'Episodes Watched' },
      { value: totalH >= 48 ? `${totalD}d` : `${totalH}h`, label: 'Watch Time' },
    ].map(({ value, label }) => `
        <div class="bg-zinc-900/60 border border-zinc-800 rounded-2xl p-5 text-center">
          <p class="text-3xl font-black text-zinc-100 tracking-tight">${value}</p>
          <p class="text-xs text-zinc-500 font-bold uppercase tracking-widest mt-1">${label}</p>
        </div>
      `).join('');
  }

  // Activity chart
  const isDaily = data.granularity === 'day';
  const activityChart = document.getElementById('activity-chart');
  if (activityChart) {
    let activityData = since && until
      ? fillActivityGaps(data.watch_activity, since, until, isDaily)
      : data.watch_activity.slice(-24);
    activityChart.classList.remove('animate-pulse', 'bg-zinc-800/50', 'rounded-xl');
    if (activityData.length > 0 && activityData.some(d => d.movies + d.episodes > 0)) {
      const maxVal = Math.max(...activityData.map(d => d.movies + d.episodes), 1);
      activityChart.style.display = 'flex';
      activityChart.style.flexDirection = 'column';
      
      const barMinWidth = isDaily ? (activityData.length > 90 ? 4 : 10) : 32;
      
      activityChart.innerHTML = `
          <div id="activity-info" style="height:18px;margin-bottom:4px;display:flex;align-items:center">
            <span id="activity-info-text" style="font-size:11px;font-weight:700;color:#a1a1aa"></span>
          </div>
          <div class="overflow-x-auto scrollbar-none" style="flex:1;min-height:0">
            <div style="display:flex;flex-direction:column;min-width:max-content;height:100%;padding-bottom:4px">
              <div style="flex:1;display:flex;align-items:flex-end;gap:2px;min-height:0">
                ${activityData.map(d => {
                  const total = d.movies + d.episodes;
                  const pct = (total / maxVal) * 100;
                  const moviePct = total > 0 ? (d.movies / total) * 100 : 50;
                  const parts = d.month.split('-').map(Number);
                  const barLabel = isDaily
                    ? `${monthNames[parts[1]-1]}. ${parts[2]}, ${parts[0]}`
                    : `${monthNames[parts[1]-1]}. ${parts[0]}`;
                  return `
                    <div data-info="${barLabel}: ${d.movies} Movies, ${d.episodes} Episodes" style="flex:1;min-width:${barMinWidth}px;height:100%;display:flex;flex-direction:column;justify-content:flex-end">
                      <div style="height:${total > 0 ? Math.max(pct, 2) : 0}%;width:100%;border-radius:3px 3px 0 0;overflow:hidden;display:flex;flex-direction:column">
                        <div style="flex:${moviePct};background:rgba(14,165,233,0.8)"></div>
                        <div style="flex:${100-moviePct};background:rgba(168,85,247,0.8)"></div>
                      </div>
                    </div>
                  `;
                }).join('')}
              </div>
              <div style="display:flex;gap:2px;margin-top:4px">
                ${activityData.map(d => {
                  const label = getActivityLabel(d, isDaily, activityData.length);
                  return `<div style="flex:1;min-width:${barMinWidth}px;text-align:center"><span style="font-size:9px;font-weight:700;color:#52525b;text-transform:uppercase;letter-spacing:.04em">${label}</span></div>`;
                }).join('')}
              </div>
            </div>
          </div>
          <div style="display:flex;gap:16px;margin-top:10px;padding-top:8px;border-top:1px solid rgba(39,39,42,0.4)">
            <span style="display:flex;align-items:center;gap:6px;font-size:10px;font-weight:700;color:#71717a;text-transform:uppercase;letter-spacing:.1em"><span style="width:8px;height:8px;border-radius:2px;background:rgba(14,165,233,0.8);display:inline-block"></span>Movies</span>
            <span style="display:flex;align-items:center;gap:6px;font-size:10px;font-weight:700;color:#71717a;text-transform:uppercase;letter-spacing:.1em"><span style="width:8px;height:8px;border-radius:2px;background:rgba(168,85,247,0.8);display:inline-block"></span>Episodes</span>
          </div>
        `;
      const infoText = document.getElementById('activity-info-text');
      activityChart.addEventListener('mouseover', e => {
        const bar = e.target.closest('[data-info]');
        if (bar && infoText) infoText.textContent = bar.dataset.info;
      });
      activityChart.addEventListener('mouseleave', () => {
        if (infoText) infoText.textContent = '';
      });
      activityChart.addEventListener('touchstart', e => {
        const bar = e.target.closest('[data-info]');
        if (bar && infoText) infoText.textContent = bar.dataset.info;
      }, { passive: true });
    } else {
      activityChart.innerHTML = '<p style="color:#52525b;font-size:14px;font-weight:500;text-align:center;padding-top:40px">No watch history for the selected period.</p>';
    }
  }

  // Watch time chart
  const watchTimeChart = document.getElementById('watch-time-chart');
  if (watchTimeChart) {
    let wtData = since && until
      ? (() => {
          const dataMap = Object.fromEntries((data.watch_time_activity || []).map(d => [d.month, d]));
          const filled = [];
          if (isDaily) {
            const cur = new Date(since); cur.setHours(0,0,0,0);
            const end = new Date(until); end.setHours(0,0,0,0);
            while (cur <= end) {
              const key = _fmt(cur);
              filled.push(dataMap[key] || { month: key, movie_minutes: 0, show_minutes: 0 });
              cur.setDate(cur.getDate() + 1);
            }
          } else {
            const [sy, sm] = since.split('-').map(Number);
            const [uy, um] = until.split('-').map(Number);
            let cy = sy, cm = sm;
            while (cy < uy || (cy === uy && cm <= um)) {
              const key = `${cy}-${String(cm).padStart(2,'0')}`;
              filled.push(dataMap[key] || { month: key, movie_minutes: 0, show_minutes: 0 });
              if (++cm > 12) { cm = 1; cy++; }
            }
          }
          return filled;
        })()
      : (data.watch_time_activity || []).slice(-24);

    watchTimeChart.classList.remove('animate-pulse', 'bg-zinc-800/50', 'rounded-xl');

    const fmtTime = (mins) => {
      if (!mins) return '0m';
      const h = Math.floor(mins / 60), m = mins % 60;
      return h > 0 ? (m > 0 ? `${h}h ${m}m` : `${h}h`) : `${m}m`;
    };

    if (wtData.length > 0 && wtData.some(d => d.movie_minutes + d.show_minutes > 0)) {
      const maxVal = Math.max(...wtData.map(d => d.movie_minutes + d.show_minutes), 1);
      const barMinWidth = isDaily ? (wtData.length > 90 ? 4 : 10) : 32;
      watchTimeChart.style.display = 'flex';
      watchTimeChart.style.flexDirection = 'column';
      watchTimeChart.innerHTML = `
          <div id="wt-info" style="height:18px;margin-bottom:4px;display:flex;align-items:center">
            <span id="wt-info-text" style="font-size:11px;font-weight:700;color:#a1a1aa"></span>
          </div>
          <div class="overflow-x-auto scrollbar-none" style="flex:1;min-height:0">
            <div style="display:flex;flex-direction:column;min-width:max-content;height:100%;padding-bottom:4px">
              <div style="flex:1;display:flex;align-items:flex-end;gap:2px;min-height:0">
                ${wtData.map(d => {
                  const total = d.movie_minutes + d.show_minutes;
                  const pct = (total / maxVal) * 100;
                  const moviePct = total > 0 ? (d.movie_minutes / total) * 100 : 50;
                  const parts = d.month.split('-').map(Number);
                  const barLabel = isDaily
                    ? `${monthNames[parts[1]-1]}. ${parts[2]}, ${parts[0]}`
                    : `${monthNames[parts[1]-1]}. ${parts[0]}`;
                  const tooltip = `${barLabel}: ${fmtTime(total)} total · ${fmtTime(d.movie_minutes)} movies · ${fmtTime(d.show_minutes)} shows`;
                  return `
                    <div data-info="${tooltip}" style="flex:1;min-width:${barMinWidth}px;height:100%;display:flex;flex-direction:column;justify-content:flex-end">
                      <div style="height:${total > 0 ? Math.max(pct, 2) : 0}%;width:100%;border-radius:3px 3px 0 0;overflow:hidden;display:flex;flex-direction:column">
                        <div style="flex:${moviePct};background:rgba(14,165,233,0.8)"></div>
                        <div style="flex:${100-moviePct};background:rgba(168,85,247,0.8)"></div>
                      </div>
                    </div>
                  `;
                }).join('')}
              </div>
              <div style="display:flex;gap:2px;margin-top:4px">
                ${wtData.map(d => {
                  const label = getActivityLabel(d, isDaily, wtData.length);
                  return `<div style="flex:1;min-width:${barMinWidth}px;text-align:center"><span style="font-size:9px;font-weight:700;color:#52525b;text-transform:uppercase;letter-spacing:.04em">${label}</span></div>`;
                }).join('')}
              </div>
            </div>
          </div>
          <div style="display:flex;gap:16px;margin-top:10px;padding-top:8px;border-top:1px solid rgba(39,39,42,0.4)">
            <span style="display:flex;align-items:center;gap:6px;font-size:10px;font-weight:700;color:#71717a;text-transform:uppercase;letter-spacing:.1em"><span style="width:8px;height:8px;border-radius:2px;background:rgba(14,165,233,0.8);display:inline-block"></span>Movies</span>
            <span style="display:flex;align-items:center;gap:6px;font-size:10px;font-weight:700;color:#71717a;text-transform:uppercase;letter-spacing:.1em"><span style="width:8px;height:8px;border-radius:2px;background:rgba(168,85,247,0.8);display:inline-block"></span>Shows</span>
          </div>
        `;
      const wtInfoText = document.getElementById('wt-info-text');
      watchTimeChart.addEventListener('mouseover', e => {
        const bar = e.target.closest('[data-info]');
        if (bar && wtInfoText) wtInfoText.textContent = bar.dataset.info;
      });
      watchTimeChart.addEventListener('mouseleave', () => {
        if (wtInfoText) wtInfoText.textContent = '';
      });
      watchTimeChart.addEventListener('touchstart', e => {
        const bar = e.target.closest('[data-info]');
        if (bar && wtInfoText) wtInfoText.textContent = bar.dataset.info;
      }, { passive: true });
    } else {
      watchTimeChart.innerHTML = '<p style="color:#52525b;font-size:14px;font-weight:500;text-align:center;padding-top:40px">No watch time data for the selected period.</p>';
    }
  }

  renderBreakdownCharts(data);
  renderTopStats(data);
} // end renderStats

// ── Most Watched: top titles, networks and people (#124) ──────────────────
function renderTopStats(data) {
  const section = document.getElementById('top-stats');
  if (!section) return;
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmtH = (min) => { const h = Math.floor(min / 60); const m = min % 60; return h > 0 ? (m > 0 ? `${h}h ${m}m` : `${h}h`) : `${m}m`; };
  const card = (id, has) => { const el = document.getElementById(id + '-card'); if (el) el.style.display = has ? '' : 'none'; return has; };

  const posters = (items) => (items || []).filter(it => it.title).map(it => `
      <a href="${it.episodes != null ? '/show/' + it.tmdb_id : '/media/movie/' + it.tmdb_id}" class="group block">
        <div class="aspect-[2/3] rounded-xl overflow-hidden bg-zinc-800">
          ${it.poster_path ? `<img src="${esc(it.poster_path)}" alt="${esc(it.title)}" loading="lazy" class="w-full h-full object-cover group-hover:scale-105 transition-transform">` : ''}
        </div>
        <div class="text-xs text-zinc-300 font-semibold mt-1.5 truncate">${esc(it.title)}</div>
        <div class="text-[11px] text-zinc-500">${it.plays} play${it.plays === 1 ? '' : 's'} · ${fmtH(it.minutes || 0)}</div>
      </a>`).join('');
  document.getElementById('top-shows').innerHTML = posters(data.top_shows);
  document.getElementById('top-movies').innerHTML = posters(data.top_movies);

  document.getElementById('top-networks').innerHTML = (data.top_networks || []).map(n => `
      <div class="flex items-center gap-2 bg-white/90 rounded-lg px-3 py-2" title="${esc(n.name)} · ${n.plays} plays">
        ${n.logo_path ? `<img src="https://image.tmdb.org/t/p/w92${esc(n.logo_path)}" alt="${esc(n.name)}" class="h-5 object-contain">` : `<span class="text-sm font-bold text-zinc-800">${esc(n.name)}</span>`}
        <span class="text-xs font-semibold text-zinc-500">${n.plays}</span>
      </div>`).join('');

  const chips = (items, linked) => (items || []).map(p => {
    const tag = linked ? 'a' : 'span';
    const href = linked ? ` href="/person/${p.id}"` : '';
    return `
      <${tag}${href} class="inline-flex items-center gap-1.5 bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-1.5${linked ? ' hover:border-blue-500/50 hover:bg-zinc-800/60 transition-colors' : ''}" title="${esc(p.name)} · ${p.titles} title${p.titles === 1 ? '' : 's'} · ${p.plays} play${p.plays === 1 ? '' : 's'}">
        <span class="text-sm font-semibold text-zinc-200">${esc(p.name)}</span>
        <span class="text-[11px] font-semibold text-blue-400 bg-blue-500/10 rounded-full px-1.5 py-0.5">${p.titles}</span>
      </${tag}>`;
  }).join('');
  const people = data.top_people || {};
  document.getElementById('top-actors').innerHTML = chips(people.actors, true);
  document.getElementById('top-directors').innerHTML = chips(people.directors, true);
  document.getElementById('top-writers').innerHTML = chips(people.writers, true);
  document.getElementById('top-studios').innerHTML = chips(people.studios, false);

  const any = [
    card('top-shows', (data.top_shows || []).length > 0),
    card('top-movies', (data.top_movies || []).length > 0),
    card('top-networks', (data.top_networks || []).length > 0),
    card('top-actors', (people.actors || []).length > 0),
    card('top-directors', (people.directors || []).length > 0),
    card('top-writers', (people.writers || []).length > 0),
    card('top-studios', (people.studios || []).length > 0),
  ].some(Boolean);
  section.style.display = any ? '' : 'none';
}

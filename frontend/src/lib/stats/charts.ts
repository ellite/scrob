import { formatMinutes } from "./format";

export function renderBreakdownCharts(data) {
  const ratingChart = document.getElementById('rating-chart');
  if (ratingChart) {
    const hasRatings = data.rating_distribution.length > 0;
    if (hasRatings) {
      const ratingMap = new Map();
      for (let r = 1; r <= 10; r++) ratingMap.set(r, 0);
      data.rating_distribution.forEach(d => ratingMap.set(d.rating, d.count));
      const fullDist = Array.from(ratingMap, ([rating, count]) => ({ rating, count }));
      const maxCount = Math.max(...fullDist.map(d => d.count), 1);
      ratingChart.classList.remove('animate-pulse', 'bg-zinc-800/50', 'rounded-xl');
      ratingChart.style.display = 'flex';
      ratingChart.style.flexDirection = 'column';
      ratingChart.innerHTML = `
          <div style="flex:1;display:flex;align-items:flex-end;gap:4px;min-height:0">
            ${fullDist.map(d => {
              const pct = (d.count / maxCount) * 100;
              return `
                <div style="flex:1;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center">
                  <span style="font-size:9px;font-weight:800;color:${d.count > 0 ? 'rgba(245,158,11,0.85)' : 'transparent'};margin-bottom:2px;line-height:1">${d.count > 0 ? d.count : '0'}</span>
                  <div style="height:${d.count > 0 ? Math.max(pct, 3) : 2}%;width:100%;background:${d.count > 0 ? 'rgba(245,158,11,0.7)' : 'rgba(63,63,70,0.4)'};border-radius:3px 3px 0 0"></div>
                </div>
              `;
            }).join('')}
          </div>
          <div style="display:flex;gap:4px;margin-top:4px">
            ${fullDist.map(d => `<div style="flex:1;text-align:center"><span style="font-size:9px;color:#52525b;font-weight:700;white-space:nowrap">${d.rating}★</span></div>`).join('')}
          </div>
        `;
    } else {
      ratingChart.classList.remove('animate-pulse', 'bg-zinc-800/50', 'rounded-xl');
      ratingChart.innerHTML = '<p style="color:#52525b;font-size:14px;font-weight:500;text-align:center;padding-top:40px">No ratings for the selected period.</p>';
    }
  }

  // Most watched genres
  const genresContainer = document.getElementById('genres-container');
  if (genresContainer) {
    genresContainer.classList.remove('animate-pulse');
    const renderGenreList = (title, genres, bgColor, textColor) => {
      const max = genres.length > 0 ? Math.max(...genres.map(g => g.count)) : 1;
      return `
          <div class="space-y-3">
            <h4 class="text-[10px] font-black text-zinc-500 uppercase tracking-widest">${title}</h4>
            <div class="space-y-2">
              ${genres.length > 0 ? genres.map(g => {
                const pct = (g.count / max) * 100;
                return `
                  <div class="relative flex items-center justify-between px-3 py-2 bg-zinc-800/30 rounded-lg overflow-hidden">
                    <div class="absolute inset-0" style="width:${pct}%;background:${bgColor};pointer-events:none"></div>
                    <span class="relative text-xs font-bold text-zinc-300">${g.name}</span>
                    <span class="relative text-xs font-black" style="color:${textColor}">${g.count}</span>
                  </div>
                `;
              }).join('') : `<p class="text-xs text-zinc-600 italic">No data</p>`}
            </div>
          </div>
        `;
    };
    genresContainer.innerHTML =
      renderGenreList('Movies', data.top_movie_genres || [], 'rgba(14,165,233,0.1)', '#38bdf8') +
      renderGenreList('Shows', data.top_show_genres || [], 'rgba(168,85,247,0.1)', '#c084fc');
  }

  // Watch time breakdown
  const watchTimeBreakdown = document.getElementById('watch-time-breakdown');
  if (watchTimeBreakdown) {
    watchTimeBreakdown.classList.remove('animate-pulse');
    watchTimeBreakdown.innerHTML = [
      { label: 'Movies', value: data.movie_watch_minutes, textColor: '#38bdf8' },
      { label: 'Shows',  value: data.show_watch_minutes,  textColor: '#c084fc' },
    ].map(({ label, value, textColor }) => `
        <div class="flex items-center justify-between bg-zinc-800/40 rounded-xl px-4 py-3">
          <span class="text-sm font-bold text-zinc-300">${label}</span>
          <span style="color:${textColor}" class="font-black text-lg">${formatMinutes(value)}</span>
        </div>
      `).join('');
  }

  // Avg ratings
  const avgRatings = document.getElementById('avg-ratings');
  if (avgRatings) {
    avgRatings.classList.remove('animate-pulse');
    avgRatings.innerHTML = [
      { label: 'Movies', value: data.avg_movie_rating, textColor: '#38bdf8' },
      { label: 'Shows', value: data.avg_show_rating,   textColor: '#c084fc' },
    ].map(({ label, value, textColor }) => `
        <div class="flex items-center justify-between bg-zinc-800/40 rounded-xl px-4 py-3">
          <span class="text-sm font-bold text-zinc-300">${label}</span>
          ${value != null
            ? `<span class="flex items-center gap-1.5 font-black text-lg" style="color:${textColor}"><span class="text-sm text-yellow-400">★</span>${value.toFixed(1)}</span>`
            : `<span class="text-zinc-600 text-sm font-medium">No ratings</span>`
          }
        </div>
      `).join('');
  }

  // Weekday activity chart
  const weekdayChart = document.getElementById('weekday-chart');
  if (weekdayChart && data.weekday_activity) {
    weekdayChart.classList.remove('animate-pulse', 'bg-zinc-800/50', 'rounded-xl');
    const hasActivity = data.weekday_activity.some(d => d.avg > 0);
    if (hasActivity) {
      const maxAvg = Math.max(...data.weekday_activity.map(d => d.avg), 0.1);
      const peakDay = data.weekday_activity.reduce((a, b) => a.avg >= b.avg ? a : b).day;
      weekdayChart.style.display = 'flex';
      weekdayChart.style.gap = '6px';
      weekdayChart.style.alignItems = 'flex-end';
      weekdayChart.innerHTML = data.weekday_activity.map(d => {
        const pct = (d.avg / maxAvg) * 100;
        const isPeak = d.day === peakDay;
        const color = isPeak ? 'rgba(14,165,233,0.9)' : 'rgba(63,63,70,0.8)';
        return `
            <div style="flex:1;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:6px">
              <span style="font-size:10px;font-weight:700;color:${isPeak ? 'rgba(14,165,233,0.9)' : '#52525b'}">${d.avg > 0 ? d.avg.toFixed(1) : ''}</span>
              <div style="height:${Math.max(pct, 3)}%;width:100%;background:${color};border-radius:4px 4px 0 0" title="${d.day}: avg ${d.avg} items/week"></div>
              <span style="font-size:11px;font-weight:800;color:${isPeak ? '#e4e4e7' : '#71717a'};text-transform:uppercase;letter-spacing:.05em">${d.day}</span>
            </div>
          `;
      }).join('');
    } else {
      weekdayChart.style.display = '';
      weekdayChart.style.gap = '';
      weekdayChart.style.alignItems = '';
      weekdayChart.innerHTML = '<p style="color:#52525b;font-size:14px;font-weight:500;text-align:center;padding-top:40px">No watch history for the selected period.</p>';
    }
  }

  // Collection breakdown
  const collectionBreakdown = document.getElementById('collection-breakdown');
  if (collectionBreakdown) {
    collectionBreakdown.classList.remove('animate-pulse');
    collectionBreakdown.innerHTML = [
      { label: 'Movies Collected',   value: data.movies_collected.toLocaleString(),   textColor: '#38bdf8' },
      { label: 'Shows Collected',    value: data.shows_collected.toLocaleString(),    textColor: '#c084fc' },
      { label: 'Episodes Collected', value: data.episodes_collected.toLocaleString(), textColor: '#818cf8' },
    ].map(({ label, value, textColor }) => `
        <div class="flex items-center justify-between bg-zinc-800/40 rounded-xl px-4 py-3">
          <span class="text-sm font-bold text-zinc-300">${label}</span>
          <span style="color:${textColor}" class="font-black text-lg">${value}</span>
        </div>
      `).join('');
  }

  // Collection pie charts
  const pieContainer = document.getElementById('collection-pies');
  if (pieContainer) {
    const C = 2 * Math.PI * 38; // circumference for r=38

    function donut(pct, colorA, colorB, title, labelA, labelB, countA, countB) {
      const arc = Math.max(Math.min(pct / 100 * C, C - 1), 1);
      return `
          <div class="flex flex-col items-center gap-6">
            <p class="text-[10px] font-black text-zinc-500 uppercase tracking-widest text-center">${title}</p>
            <svg viewBox="0 0 100 100" width="80" height="80" style="transform:rotate(-90deg)">
              <circle cx="50" cy="50" r="38" fill="none" stroke="${colorB}" stroke-width="14"/>
              <circle cx="50" cy="50" r="38" fill="none" stroke="${colorA}" stroke-width="14"
                stroke-dasharray="${arc} ${C}" stroke-dashoffset="0"/>
            </svg>
            <div class="flex flex-col gap-1 w-full">
              <div class="flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-sm shrink-0" style="background:${colorA}"></span>
                <span class="text-[11px] font-bold text-zinc-500 flex-1">${labelA}</span>
                <span class="text-[11px] font-black text-zinc-800 dark:text-zinc-100">${countA.toLocaleString()}</span>
              </div>
              <div class="flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-sm shrink-0" style="background:${colorB}"></span>
                <span class="text-[11px] font-bold text-zinc-500 flex-1">${labelB}</span>
                <span class="text-[11px] font-black text-zinc-800 dark:text-zinc-100">${countB.toLocaleString()}</span>
              </div>
            </div>
          </div>
        `;
    }

    const totalColl = data.movies_collected + data.shows_collected;
    const moviesPct = totalColl > 0 ? data.movies_collected / totalColl * 100 : 0;
    const mwPct     = data.movies_collected > 0 ? data.movies_watched_collected / data.movies_collected * 100 : 0;
    const swPct     = data.shows_collected  > 0 ? data.shows_watched_collected  / data.shows_collected  * 100 : 0;

    pieContainer.classList.remove('animate-pulse');
    pieContainer.innerHTML =
      donut(moviesPct, 'rgba(14,165,233,0.85)', 'rgba(168,85,247,0.7)', 'Movies vs Shows', 'Movies',  'Shows',     data.movies_collected,         data.shows_collected) +
      donut(mwPct,     'rgba(34,197,94,0.85)',  'rgba(63,63,70,0.6)',   'Movies Watched',  'Watched', 'Unwatched', data.movies_watched_collected, data.movies_unwatched_collected) +
      donut(swPct,     'rgba(34,197,94,0.85)',  'rgba(63,63,70,0.6)',   'Shows Watched',   'Watched', 'Unwatched', data.shows_watched_collected,  data.shows_unwatched_collected);
  }
}

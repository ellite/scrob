// Selectable rating source - client side.
//
// Loaded right after /external-ratings.js (which sets window.__extRatings)
// and only when the user picked a source other than TMDB. It
//  - re-renders poster badges drawn by client-side scripts (Trending, Next
//    Up, lists) from the cached values, before they are painted;
//  - fetches titles that aren't cached yet (or are older than a day) when
//    their poster scrolls into view, at most 4 requests at a time.
(() => {
  const D = window.__extRatings;
  if (!D || window.__extRatingsStarted) return;
  window.__extRatingsStarted = true;

  const values = Object.assign({}, D.values);
  const missing = new Set(D.missing);
  const stale = new Set(D.stale);
  const meta = D.meta;
  const inflight = new Map();
  let active = 0;
  const waiting = [];
  const slot = () => (active < 4 ? (active++, Promise.resolve()) : new Promise((r) => waiting.push(() => { active++; r(); })));
  const release = () => { active--; const next = waiting.shift(); if (next) next(); };

  const fmt = (v) => (meta.fmt === "pct" ? Math.round(v) + "%" : meta.fmt === "int" ? String(Math.round(v)) : Number(v).toFixed(1));
  const star = (r) => '<span class="text-yellow-400 inline-flex items-center gap-1"><span class="leading-none">★</span>' + r + "</span>";
  const sep = '<span class="ext-sep w-px h-3 bg-zinc-600 mx-0.5"></span>';
  const ext = (text, pending) =>
    '<span class="ext-rt inline-flex items-center gap-1 ' + (pending ? "text-zinc-500 animate-pulse" : "text-zinc-100") +
    '" title="' + meta.name + '">' + meta.prefix + text + "</span>";

  function badgeHtml(tmdb, state, value) {
    // state: "value" | "missing" | "pending"
    if (state === "value") return D.showTmdb && tmdb ? star(tmdb) + sep + ext(fmt(value)) : ext(fmt(value));
    if (state === "missing") {
      if (!tmdb) return ext("–");
      return D.showTmdb ? star(tmdb) + sep + ext("–") : star(tmdb);
    }
    return D.showTmdb && tmdb ? star(tmdb) + sep + ext("…", true) : ext("…", true);
  }

  function keyFor(b) {
    if (b.dataset.extKey) return b.dataset.extKey;
    if (b.dataset.extKind && b.dataset.extId) return (b.dataset.extKind === "movie" ? "m" : "s") + b.dataset.extId;
    const card = b.closest("[data-card]");
    if (!card) return "";
    const t = card.dataset.mediaType;
    if (t === "movie" && card.dataset.tmdbId) return "m" + card.dataset.tmdbId;
    if (t === "series" || t === "episode") {
      const id = card.dataset.showTmdbId || (t === "series" ? card.dataset.tmdbId : "");
      return id ? "s" + id : "";
    }
    return "";
  }

  function tmdbFor(b) {
    if (b.dataset.tmdbRating !== undefined) return b.dataset.tmdbRating;
    const m = (b.textContent || "").match(/★\s*([\d.]+)/);
    b.dataset.tmdbRating = m ? m[1] : "";
    return b.dataset.tmdbRating;
  }

  function render(b, key) {
    const tmdb = tmdbFor(b);
    const state = values[key] !== undefined ? "value" : missing.has(key) ? "missing" : "pending";
    b.classList.remove("text-yellow-400", "hidden");
    b.innerHTML = badgeHtml(tmdb, state, values[key]);
    return state;
  }

  function fetchTitle(key) {
    if (!inflight.has(key)) {
      const path = (key[0] === "m" ? "movie/" : "show/") + key.slice(1);
      inflight.set(key, slot().then(() =>
        fetch("/api/proxy/external-ratings/" + path, { credentials: "same-origin" })
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => {
            if (!d) return;
            const entry = d.ratings && d.ratings[D.source];
            if (entry && entry.value != null) { values[key] = entry.value; missing.delete(key); }
            else { delete values[key]; missing.add(key); }
            stale.delete(key);
          })
          .catch(() => {})
          .finally(release)));
    }
    return inflight.get(key);
  }

  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const b = e.target;
      io.unobserve(b);
      const key = b.dataset.extKeyResolved;
      fetchTitle(key).then(() => {
        // Unchanged pending after a failed request: fall back to TMDB rather than spin forever.
        if (values[key] === undefined && !missing.has(key)) missing.add(key);
        render(b, key);
      });
    }
  }, { rootMargin: "400px" });

  function handle(b) {
    if (b.dataset.extSeen) return;
    b.dataset.extSeen = "1";
    const key = keyFor(b);
    if (!key) return;
    b.dataset.extKeyResolved = key;
    // Server-rendered badges are already right; client-drawn ones (still the plain ★) are re-rendered now.
    const state = b.querySelector(".ext-rt") ? (values[key] !== undefined ? "value" : missing.has(key) ? "missing" : "pending") : render(b, key);
    if (state === "pending" || stale.has(key) || b.dataset.extStale) io.observe(b);
  }

  const scan = () => document.querySelectorAll(".ext-rate:not([data-ext-seen])").forEach(handle);
  new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", scan);
  scan();
})();

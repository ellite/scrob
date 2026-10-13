// Lokal patch (Popcornmeter): byter/kompletterar betygsbrickor med Rotten Tomatoes Popcornmeter.
// Data och ikoner kommer från /ext-ratings.js (laddas före den här filen).
(() => {
  var D = window.__extRatingsData;
  if (!D || window.__extRatings) return;
  window.__extRatings = true;
  // Kompakta nycklar från servern: "m123" = movie/123, "s45" = show/45.
  const full = (k) => (k[0] === "m" ? "movie/" : "show/") + k.slice(1);
  const map = {};
  for (const k in D.k) map[full(k)] = D.k[k];
  const big = new Set(D.b.map(full));
  const stale = new Set(D.o.map(full));
  const icons = D.i;
  const ensureSprite = () => {
    if (!document.getElementById("ext-icon-sprite") && document.body) document.body.insertAdjacentHTML("afterbegin", D.sprite);
  };
  ensureSprite();
  const inflight = new Map();
  let active = 0;
  const waiting = [];
  const slot = () => active < 4 ? (active++, Promise.resolve()) : new Promise((r) => waiting.push(() => { active++; r(); }));
  const release = () => { active--; const n = waiting.shift(); if (n) n(); };
  const COLORS = ["text-yellow-400", "text-red-400", "text-green-400", "text-cyan-400", "text-zinc-100", "text-zinc-500", "animate-pulse"];
  const FALLBACK = { fresh: "🍅", rotten: "🟢", noScore: "·", banner: "🍅" };

  function color(p) {
    return "text-zinc-100"; // mjukvit på alla nivåer
  }
  function icon(name) {
    return icons[name]
      ? '<svg class="w-3.5 h-3.5 shrink-0" aria-hidden="true"><use href="#ext-icon-' + name + '"></use></svg>'
      : FALLBACK[name];
  }
  function iconFor(p, isBig) {
    if (p == null) return "noScore";
    if (isBig && p >= 90) return "banner";
    return p >= 60 ? "fresh" : "rotten";
  }
  function target(b) {
    const card = b.closest("[data-card]");
    const t = card && card.dataset.mediaType;
    const kind = b.dataset.extKind || (t === "movie" ? "movie" : (t === "series" || t === "episode") ? "show" : "");
    const id = b.dataset.extId || (kind === "show"
      ? (card && card.dataset.showTmdbId) || (t === "series" ? card && card.dataset.tmdbId : "")
      : card && card.dataset.tmdbId) || "";
    return kind && id ? kind + "/" + id : "";
  }
  // Delar brickan i ★ TMDB | RT. Returnerar RT-delen, som är det enda skriptet ändrar.
  function rtPart(b) {
    let rt = b.querySelector(".ext-rt");
    if (rt) return rt;
    const m = (b.textContent || "").match(/★\s*([\d.]+)/);
    b.classList.remove(...COLORS);
    b.innerHTML = (m ? '<span class="text-yellow-400 inline-flex items-center gap-1"><span class="text-xs leading-none">★</span>' + m[1] + '</span><span class="ext-sep w-px h-3 bg-zinc-600 mx-0.5"></span>' : "")
      + '<span class="ext-rt inline-flex items-center gap-1"></span>';
    return b.querySelector(".ext-rt");
  }
  function show(b, p, isBig) {
    const rt = rtPart(b);
    b.classList.remove("hidden");
    rt.classList.remove(...COLORS);
    rt.classList.add(color(p));
    rt.title = "Rotten Tomatoes Popcornmeter";
    rt.innerHTML = icon(iconFor(p, isBig)) + p + "%";
    b.dataset.extDone = "1";
  }
  function noScore(b) {
    b.dataset.extDone = "1";
    if (b.classList.contains("hidden")) { b.remove(); return; }
    const rt = rtPart(b);
    rt.classList.remove(...COLORS);
    rt.classList.add("text-zinc-100");
    rt.title = "Inget Popcornmeter-betyg";
    rt.innerHTML = icon("noScore") + "–";
  }
  function pending(b) {
    const rt = rtPart(b);
    rt.classList.remove(...COLORS);
    rt.classList.add("text-zinc-500", "animate-pulse");
    rt.innerHTML = icon("noScore") + "…";
  }
  function fetchPop(key) {
    if (!inflight.has(key)) {
      inflight.set(key, slot().then(() =>
        fetch("/api/proxy/ext-ratings/" + key, { credentials: "same-origin" })
          .then((r) => r.ok ? r.json() : null)
          .then((d) => d && d.popcorn != null && isFinite(+d.popcorn) ? { p: +d.popcorn, big: +d.popcorn >= 90 && +d.votes >= 500 } : null)
          .catch(() => null)
          .finally(release)));
    }
    return inflight.get(key);
  }
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const b = e.target;
      io.unobserve(b);
      const key = target(b);
      const reval = b.dataset.extReval === "1";
      fetchPop(key).then((r) => {
        stale.delete(key);
        if (r == null) return reval ? undefined : noScore(b);
        map[key] = r.p;
        if (r.big) big.add(key); else big.delete(key);
        show(b, r.p, r.big);
      });
    }
  }, { rootMargin: "400px" });

  function handle(b) {
    if (b.dataset.extSeen) return;
    b.dataset.extSeen = "1";
    const key = target(b);
    if (!key) return;
    // Visa sparat betyg direkt; är det gammalt hämtas det om när kortet syns.
    const revalidate = () => { if (stale.has(key)) { b.dataset.extReval = "1"; io.observe(b); } };
    if (b.dataset.extDone) return revalidate();
    if (map[key] != null) { show(b, map[key], big.has(key)); return revalidate(); }
    if (!b.classList.contains("hidden")) pending(b);
    io.observe(b);
  }
  function scan() { document.querySelectorAll(".ext-rate:not([data-ext-seen])").forEach(handle); }

  new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", scan);
  document.addEventListener("astro:page-load", () => { ensureSprite(); scan(); });
  scan();
})();

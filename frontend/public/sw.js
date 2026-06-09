// Scrob — Service Worker
// Strategy:
//   - Static assets (JS/CSS/fonts/icons): NetworkFirst, cached for offline fallback
//   - TMDB / Proxy images: bypass service worker (fall through to native network/disk cache)
//   - /api/proxy/*: NetworkOnly — library data must always be fresh
//   - Navigation (HTML pages): NetworkFirst, offline fallback if all fail

const SHELL_CACHE  = 'scrob-shell-v2';

// ── Install ───────────────────────────────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(c => c.add('/offline.html'))
  );
  self.skipWaiting();
});

// ── Activate — prune old caches ───────────────────────────────────────────────
self.addEventListener('activate', (event) => {
  const keep = [SHELL_CACHE];
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => !keep.includes(k)).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle GET — leave POST/PATCH/DELETE to the network
  if (request.method !== 'GET') return;

  // TMDB images and proxy images: bypass service worker entirely to use native browser HTTP cache (much faster)
  if (url.hostname === 'image.tmdb.org' || url.pathname.startsWith('/api/proxy/media/image/')) {
    return; // fall through to browser default (native network/disk cache)
  }

  // API calls: always network, never cache
  if (url.pathname.startsWith('/api/')) {
    return; // fall through to browser default (network)
  }

  // Web manifest: NetworkFirst to handle potential auth redirects / CORS correctly
  if (url.pathname.endsWith('.webmanifest')) {
    event.respondWith(networkFirstWithOffline(request));
    return;
  }

  // Static assets (hashed JS/CSS/fonts/icons in /_astro/): network-first so
  // content is always fresh (avoids stale cache in dev and after deploys).
  // Cache is kept as an offline fallback only.
  if (url.pathname.startsWith('/_astro/') || isStaticAsset(url.pathname)) {
    event.respondWith(networkFirstWithCache(request, SHELL_CACHE));
    return;
  }

  // Navigation (HTML pages): network-first, offline fallback
  if (request.mode === 'navigate') {
    event.respondWith(networkFirstWithOffline(request));
    return;
  }
});

// ── Strategies ────────────────────────────────────────────────────────────────

async function networkFirstWithCache(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    return cached ?? Response.error();
  }
}

async function networkFirstWithOffline(request) {
  try {
    const response = await fetch(request);
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    return caches.match('/offline.html');
  }
}

function isStaticAsset(pathname) {
  return /\.(js|css|woff2?|ico|png|svg|webp|jpg|jpeg|webmanifest)$/.test(pathname);
}

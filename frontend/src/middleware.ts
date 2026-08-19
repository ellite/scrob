import { defineMiddleware } from "astro:middleware";
import { api } from "./lib/api";

const PUBLIC_ROUTES = ["/login", "/register", "/logout", "/oidc-callback", "/oidc-start", "/site.webmanifest", "/favicon.ico", "/favicon.svg", "/apple-touch-icon.png", "/sw.js", "/offline.html"];
const PUBLIC_PREFIXES = ["/auth/activate/", "/forgot-password", "/reset-password/", "/api/proxy/webhooks/", "/api/proxy/auth/has-users", "/api/proxy/auth/bootstrap-restore", "/api/proxy/media/stream/", "/api/proxy/radarr-compat/", "/api/proxy/sonarr-compat/"];
// Matches /profile/{id} (someone else's public profile page) but not the bare
// /profile page (the logged-in user's own profile management), which must stay gated.
const PUBLIC_PROFILE_PAGE_RE = /^\/profile\/\d+\/?$/;
// The profile page's <img> tag hits this proxy path directly. It has no file
// extension, so it doesn't fall under isStaticAsset below like TMDB poster
// URLs do, and needs the same admin-gated anonymous allowance as the page itself.
const PUBLIC_AVATAR_PROXY_RE = /^\/api\/proxy\/profile\/avatar\/\d+$/;
// Matches /list/{id} (someone else's public/friends-only list page). The list
// itself still enforces its own privacy_level server-side; this only decides
// whether a logged-out visitor gets past the gate at all.
const PUBLIC_LIST_PAGE_RE = /^\/list\/\d+\/?$/;
// The profile page's "See All" links for Top Rated Movies/Shows and Recently
// Watched Movies/Shows - same privacy model as PUBLIC_PROFILE_PAGE_RE above
// (the endpoint re-checks privacy itself).
const PUBLIC_TOP_RATED_PAGE_RE = /^\/top-rated-(?:movies|shows)\/\d+\/?$/;
const PUBLIC_RECENTLY_WATCHED_PAGE_RE = /^\/recently-watched-(?:movies|shows)\/\d+\/?$/;
// The read-only browse pages, allowed anonymously only when the admin has
// enabled logged-out navigation (Admin Settings) and a global TMDB key is set.
const PUBLIC_EXPLORE_PAGE_RE = /^\/(?:(?:movies|shows|search|lists|airing-today|discover)?|trending\/(?:movies|shows))\/?$/;
// Movie/episode and show/season/episode detail pages (TMDB- and TVDB-numbered
// variants), gated the same way as PUBLIC_EXPLORE_PAGE_RE above.
const PUBLIC_MEDIA_DETAIL_PAGE_RE =
  /^\/(?:media\/(?:movie|episode)\/\d+|show\/(?:tvdb\/)?\d+(?:\/season\/\d+(?:\/\d+)?)?|person\/\d+)\/?$/;
// The detail pages' "More like this" row and the person page's credits
// pagination are loaded client-side from these partials - same admin+
// global-key gate as the pages above, otherwise an anonymous fetch() here
// gets redirected to /login and its HTML gets injected into the page
// (fetch() follows redirects, so it looks like a normal 200 response).
const PUBLIC_RECOMMENDATIONS_PARTIAL_RE = /^\/partials\/recommendations\/?$/;
const PUBLIC_PERSON_CREDITS_PARTIAL_RE = /^\/partials\/person-credits\/?$/;
// The homepage's and /discover's data rows are loaded client-side straight
// from the backend proxy (not a same-origin partial), so the proxy path
// itself needs the same allowance - otherwise the fetch() gets redirected to
// /login and the section silently disappears (JSON.parse on the login page's
// HTML throws, caught by each row's own error handling).
const PUBLIC_MEDIA_ROWS_PROXY_RE =
  /^\/api\/proxy\/media\/(trending\/(movies|shows|trailers)|airing-today\/collected|on-air-today|now-playing|upcoming|top-rated-(movies|shows)|on-air-this-week|hidden-gems|streaming)\/?$/;
// API docs reveal the full endpoint surface and exact app version - admin-only,
// never public, regardless of the isStaticAsset check below (which would
// otherwise treat /openapi.json as a public static file just from its extension).
const ADMIN_ONLY_ROUTES = ["/docs", "/redoc", "/openapi.json"];

// Security headers added to every response.
// CSP is intentionally omitted — Astro's define:vars emits inline <script>
// blocks whose hashes change every build, making a static policy impractical.
const SECURITY_HEADERS: Record<string, string> = {
  "X-Frame-Options": "DENY",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
};

export const onRequest = defineMiddleware(async (context, next) => {
  const token = context.cookies.get("token")?.value;
  const { pathname } = context.url;

  // Requests to the backend proxy carrying a Scrob API key (header or query
  // param) skip the cookie/JWT gate below — the proxy forwards the key as-is
  // (see api/proxy/[...path].ts) and the backend's own per-endpoint auth
  // dependency decides whether that key is accepted for the route.
  const hasApiKey =
    pathname.startsWith("/api/proxy/") &&
    (context.request.headers.get("X-Api-Key") !== null || context.url.searchParams.has("api_key"));

  // Skip auth for static assets and public routes
  const isStaticAsset = /\.(js|css|woff2?|ico|png|svg|webp|jpg|jpeg|webmanifest|json|xml)$/.test(pathname);
  const isAdminOnlyRoute = ADMIN_ONLY_ROUTES.includes(pathname);
  const isPublicRoute =
    !isAdminOnlyRoute &&
    (hasApiKey || isStaticAsset || PUBLIC_ROUTES.includes(pathname) || PUBLIC_PREFIXES.some(p => pathname.startsWith(p)));

  // Anonymous access to any of these read-only pages is allowed only when the
  // admin has enabled logged-out navigation (Admin Settings) and a global
  // TMDB key is set. Profile/list pages still enforce their own privacy
  // (public/friends/private) server side - this only decides whether a
  // logged-out visitor gets past the gate at all. Fails closed (redirects to
  // login) if the check errors.
  const isAllowedAnonymousPublicPage = async () => {
    const isGatedPage =
      PUBLIC_PROFILE_PAGE_RE.test(pathname) ||
      PUBLIC_AVATAR_PROXY_RE.test(pathname) ||
      PUBLIC_LIST_PAGE_RE.test(pathname) ||
      PUBLIC_TOP_RATED_PAGE_RE.test(pathname) ||
      PUBLIC_RECENTLY_WATCHED_PAGE_RE.test(pathname) ||
      PUBLIC_EXPLORE_PAGE_RE.test(pathname) ||
      PUBLIC_MEDIA_DETAIL_PAGE_RE.test(pathname) ||
      PUBLIC_RECOMMENDATIONS_PARTIAL_RE.test(pathname) ||
      PUBLIC_PERSON_CREDITS_PARTIAL_RE.test(pathname) ||
      PUBLIC_MEDIA_ROWS_PROXY_RE.test(pathname);
    if (!isGatedPage) return false;
    try {
      const status = await api.profile.publicAccessStatus();
      return status.enable_logged_out_navigation;
    } catch {
      return false;
    }
  };

  if (token) {
    try {
      // Verify token and get user info
      const user = await api.auth.me(token);
      context.locals.user = user;
      context.locals.token = token;

      // If logged in and trying to access login/register, redirect to home
      if (pathname === "/login" || pathname === "/register") {
        return context.redirect("/", 302);
      }

      // API docs are admin-only, even for logged-in non-admin users
      if (isAdminOnlyRoute && !user.is_admin) {
        return context.redirect("/", 302);
      }
    } catch (e) {
      // Only clear the session on a genuine auth rejection (bad/expired
      // token). Any other failure here - the backend restarting, a network
      // blip, a timeout - is transient and unrelated to whether this token
      // is valid; clearing the cookie for those silently logs the user out
      // and (now that /movies and /shows are reachable anonymously) does so
      // without even an obvious redirect to explain why. Treat this one
      // request as unauthenticated and let the next request re-verify.
      const isAuthRejected = e instanceof Error && /^API 401\b/.test(e.message);
      if (isAuthRejected) {
        context.cookies.delete("token", { path: "/" });
      }
      if (!isPublicRoute && !(await isAllowedAnonymousPublicPage())) {
        return context.redirect("/login", 302);
      }
    }
  } else {
    // No token, redirect to login if not a public route
    if (!isPublicRoute && !(await isAllowedAnonymousPublicPage())) {
      return context.redirect("/login", 302);
    }
  }

  const response = await next();
  for (const [header, value] of Object.entries(SECURITY_HEADERS)) {
    response.headers.set(header, value);
  }
  return response;
});

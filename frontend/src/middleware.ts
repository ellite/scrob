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
// API docs reveal the full endpoint surface and exact app version — admin-only,
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

  // Anonymous access to someone else's profile or list page is only allowed
  // when the admin has opted into it (Admin Settings → allow_public_profiles).
  // Each route still enforces its own privacy (public/friends/private) server
  // side, this just decides whether a logged-out visitor gets past the gate
  // at all. Fails closed (redirects to login) if the check errors.
  const isAllowedAnonymousPublicPage = async () => {
    if (
      !PUBLIC_PROFILE_PAGE_RE.test(pathname) &&
      !PUBLIC_AVATAR_PROXY_RE.test(pathname) &&
      !PUBLIC_LIST_PAGE_RE.test(pathname)
    ) return false;
    try {
      const status = await api.profile.publicAccessStatus();
      return status.allow_public_profiles;
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
      // Token invalid or expired
      context.cookies.delete("token", { path: "/" });
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

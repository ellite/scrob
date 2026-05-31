import { defineMiddleware } from "astro:middleware";
import { api } from "./lib/api";

const PUBLIC_ROUTES = ["/login", "/register", "/logout", "/oidc-callback", "/oidc-start", "/site.webmanifest", "/favicon.ico", "/favicon.svg", "/apple-touch-icon.png", "/sw.js", "/offline.html"];
const PUBLIC_PREFIXES = ["/auth/activate/", "/forgot-password", "/reset-password/", "/api/proxy/webhooks/", "/api/proxy/auth/has-users", "/api/proxy/auth/bootstrap-restore", "/api/proxy/media/stream/"];

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
  
  // Skip auth for static assets and public routes
  const isStaticAsset = /\.(js|css|woff2?|ico|png|svg|webp|jpg|jpeg|webmanifest|json|xml)$/.test(pathname);
  const isPublicRoute =
    isStaticAsset || PUBLIC_ROUTES.includes(pathname) || PUBLIC_PREFIXES.some(p => pathname.startsWith(p));

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
    } catch (e) {
      // Token invalid or expired
      context.cookies.delete("token", { path: "/" });
      if (!isPublicRoute) {
        return context.redirect("/login", 302);
      }
    }
  } else {
    // No token, redirect to login if not a public route
    if (!isPublicRoute) {
      return context.redirect("/login", 302);
    }
  }

  const response = await next();
  for (const [header, value] of Object.entries(SECURITY_HEADERS)) {
    response.headers.set(header, value);
  }
  return response;
});

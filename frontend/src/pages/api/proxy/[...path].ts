import type { APIRoute } from "astro";
import { backendPath } from "../../../lib/backend-url.mjs";

// This proxy is a generic catch-all for every backend path, so a redirect
// passed straight through to the browser must be pinned to hosts we actually
// expect - otherwise a bug (or a future endpoint) returning an unexpected
// Location would have the browser follow it unchecked. Today the only
// backend route that redirects here is image serving, straight to TMDB.
const ALLOWED_REDIRECT_HOSTS = new Set(["image.tmdb.org"]);

async function handle({ params, request }: Parameters<APIRoute>[0]): Promise<Response> {
  const path = params.path ?? "";
  const search = new URL(request.url).search;
  const backendUrl = backendPath(`/${path}${search}`);

  const forwardHeaders = new Headers();

  const auth = request.headers.get("Authorization");
  if (auth) {
    forwardHeaders.set("Authorization", auth);
  } else {
    // Video elements can't set custom headers — extract JWT from the query string or session cookie instead
    const url = new URL(request.url);
    const tokenQuery = url.searchParams.get("token");
    if (tokenQuery) {
      forwardHeaders.set("Authorization", `Bearer ${tokenQuery}`);
    } else {
      const cookieStr = request.headers.get("Cookie") ?? "";
      const tokenMatch = /(?:^|;\s*)token=([^;]+)/.exec(cookieStr);
      if (tokenMatch) {
        forwardHeaders.set("Authorization", `Bearer ${decodeURIComponent(tokenMatch[1])}`);
      }
    }
  }

  // Forward full Content-Type including multipart boundary
  const ct = request.headers.get("Content-Type");
  if (ct) forwardHeaders.set("Content-Type", ct);

  // Forward Range for video seeking
  const range = request.headers.get("Range");
  if (range) forwardHeaders.set("Range", range);

  // Forward API key for Radarr/Sonarr import list compat endpoints
  const apiKey = request.headers.get("X-Api-Key");
  if (apiKey) forwardHeaders.set("X-Api-Key", apiKey);

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const body = hasBody ? await request.arrayBuffer() : undefined;

  let res: Response;
  try {
    res = await fetch(backendUrl, {
      method: request.method,
      headers: forwardHeaders,
      body,
      // Some backend routes (e.g. image serving) 3xx-redirect straight to a
      // third-party host (TMDB) when nothing is locally cached. Following
      // that here would make this dev-server process itself open the
      // outbound connection instead of the browser - if that connection
      // has trouble (flaky network, TLS/SNI issues), it throws an unhandled
      // error that took down the whole page. Passing the redirect straight
      // through lets the browser fetch it directly, the same as any other
      // cross-origin redirect, and fail gracefully (e.g. a broken <img>)
      // instead of crashing the SSR request.
      redirect: "manual",
    });
  } catch (e) {
    console.error(`Proxy request to ${backendUrl} failed:`, e);
    return new Response(null, { status: 502 });
  }

  if (res.status >= 300 && res.status < 400) {
    const location = res.headers.get("Location");
    if (location) {
      let allowed = false;
      try {
        allowed = ALLOWED_REDIRECT_HOSTS.has(new URL(location).hostname);
      } catch {
        // Relative or otherwise unparseable Location - not one of ours, reject below.
      }
      if (!allowed) {
        console.error(`Proxy refused to forward redirect to unexpected host: ${location}`);
        return new Response(null, { status: 502 });
      }
      return new Response(null, { status: res.status, headers: { Location: location } });
    }
  }

  const responseHeaders = new Headers();
  const resCt = res.headers.get("Content-Type");
  if (resCt) {
    const cleanCt = resCt.toLowerCase().trim();
    if (cleanCt === "video/x-matroska" || cleanCt === "video/mkv") {
      responseHeaders.set("Content-Type", "video/webm");
    } else {
      responseHeaders.set("Content-Type", resCt);
    }
  }

  // Forward streaming, download and caching headers
  for (const h of ["Content-Range", "Accept-Ranges", "Content-Length", "Content-Disposition", "Cache-Control", "ETag", "Last-Modified"]) {
    const v = res.headers.get(h);
    if (v) responseHeaders.set(h, v);
  }

  return new Response(res.body, { status: res.status, headers: responseHeaders });
}

export const GET: APIRoute = (ctx) => handle(ctx);
export const POST: APIRoute = (ctx) => handle(ctx);
export const PUT: APIRoute = (ctx) => handle(ctx);
export const PATCH: APIRoute = (ctx) => handle(ctx);
export const DELETE: APIRoute = (ctx) => handle(ctx);

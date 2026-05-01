import type { APIRoute } from "astro";

const BACKEND_PORT = (import.meta.env.BACKEND_PORT as string | undefined) ?? "7331";
const BACKEND = `http://localhost:${BACKEND_PORT}`;

async function handle({ params, request }: Parameters<APIRoute>[0]): Promise<Response> {
  const path = params.path ?? "";
  const search = new URL(request.url).search;
  const backendUrl = `${BACKEND}/${path}${search}`;

  const forwardHeaders = new Headers();

  const auth = request.headers.get("Authorization");
  if (auth) {
    forwardHeaders.set("Authorization", auth);
  } else {
    // Video elements can't set custom headers — extract JWT from the session cookie instead
    const cookieStr = request.headers.get("Cookie") ?? "";
    const tokenMatch = /(?:^|;\s*)token=([^;]+)/.exec(cookieStr);
    if (tokenMatch) {
      forwardHeaders.set("Authorization", `Bearer ${decodeURIComponent(tokenMatch[1])}`);
    }
  }

  // Forward full Content-Type including multipart boundary
  const ct = request.headers.get("Content-Type");
  if (ct) forwardHeaders.set("Content-Type", ct);

  // Forward Range for video seeking
  const range = request.headers.get("Range");
  if (range) forwardHeaders.set("Range", range);

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const body = hasBody ? await request.arrayBuffer() : undefined;

  const res = await fetch(backendUrl, {
    method: request.method,
    headers: forwardHeaders,
    body,
  });

  const responseHeaders = new Headers();
  const resCt = res.headers.get("Content-Type");
  if (resCt) responseHeaders.set("Content-Type", resCt);

  // Forward streaming headers so the browser can seek
  for (const h of ["Content-Range", "Accept-Ranges", "Content-Length"]) {
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

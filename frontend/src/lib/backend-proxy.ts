const BACKEND_PORT = (import.meta.env.BACKEND_PORT as string | undefined) ?? "7331";
const BACKEND = `http://localhost:${BACKEND_PORT}`;

// Passes a backend path straight through byte-for-byte. Used for FastAPI's
// built-in docs (/docs, /redoc, /openapi.json) — their generated HTML embeds
// absolute references to sibling paths (e.g. "/openapi.json"), so these must
// be served at the same root-relative path on the frontend, not nested under
// /api/proxy/, for those references to resolve correctly.
//
// These backend routes require an admin Bearer token (see main.py) — the
// middleware already confirms the caller is an admin before this ever runs,
// but the token still has to be forwarded so the backend's own check passes.
export async function proxyBackendPath(path: string, token?: string): Promise<Response> {
  const headers = new Headers();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${BACKEND}${path}`, { headers });
  const responseHeaders = new Headers();
  const contentType = res.headers.get("Content-Type");
  if (contentType) responseHeaders.set("Content-Type", contentType);
  return new Response(res.body, { status: res.status, headers: responseHeaders });
}

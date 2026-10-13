// Bootstrap data for /external-ratings-client.js: the user's rating source and
// every cached value of it. Served as a script so the browser can cache it
// (ETag -> 304) instead of every page inlining the whole map.
import type { APIRoute } from "astro";
import { api } from "../lib/api";
import { SOURCE_META, prefixHtml, type ExtSource } from "../lib/externalRatings";
import { createHash } from "node:crypto";

export const GET: APIRoute = async ({ request, locals }) => {
  const headers: Record<string, string> = {
    "Content-Type": "application/javascript; charset=utf-8",
    "Cache-Control": "private, no-cache",
  };
  if (!locals.token) return new Response("", { headers });
  let body = "";
  try {
    const m = await api.externalRatings.map(locals.token);
    if (m.source !== "tmdb" && m.source in SOURCE_META) {
      const source = m.source as ExtSource;
      const data = {
        source,
        showTmdb: m.show_tmdb,
        values: m.values,
        stale: m.stale,
        missing: m.missing,
        meta: { name: SOURCE_META[source].name, fmt: SOURCE_META[source].fmt, prefix: prefixHtml(source) },
      };
      body = `window.__extRatings=${JSON.stringify(data).replace(/</g, "\\u003c")};`;
    }
  } catch {
    body = "";
  }
  const etag = `W/"${createHash("sha1").update(body).digest("base64url")}"`;
  headers.ETag = etag;
  if (request.headers.get("if-none-match") === etag) return new Response(null, { status: 304, headers });
  return new Response(body, { headers });
};

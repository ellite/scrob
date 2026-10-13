// Lokal patch (Popcornmeter): sparade betyg + ikon-sprite som ett cachebart skript.
// Webbläsaren frågar med If-None-Match och får 304 när inget har ändrats, så
// varken betygskartan eller ikonerna behöver följa med i varje sida.
import type { APIRoute } from "astro";
import {
  popcornMap, popcornBigKeys, popcornStaleKeys, iconsAvailable, iconSprite, iconsVersion, ratingsVersion,
} from "../lib/extRatings";

const short = (k: string) => (k.startsWith("movie/") ? "m" : "s") + k.slice(k.indexOf("/") + 1);

export const GET: APIRoute = ({ request }) => {
  const map = popcornMap();
  // Timmen ingår eftersom listan över gamla betyg beror på klockan.
  const etag = `W/"ext-${ratingsVersion()}-${iconsVersion()}-${Math.floor(Date.now() / 3_600_000)}"`;
  const headers = {
    "Content-Type": "application/javascript; charset=utf-8",
    "Cache-Control": "private, no-cache",
    ETag: etag,
  };
  if (request.headers.get("if-none-match") === etag) return new Response(null, { status: 304, headers });
  const k: Record<string, number> = {};
  for (const [key, p] of Object.entries(map)) k[short(key)] = p;
  const data = {
    k,
    b: popcornBigKeys().map(short),
    o: popcornStaleKeys().map(short),
    i: iconsAvailable(),
    sprite: iconSprite(),
  };
  const body = `window.__extRatingsData=${JSON.stringify(data).replace(/</g, "\\u003c")};`;
  return new Response(body, { headers });
};

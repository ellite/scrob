/**
 * Whether a cookie set on this response may carry the `Secure` flag.
 *
 * A `Secure` cookie is silently dropped by the browser when the page was
 * loaded over plain HTTP (a LAN address, a proxy that does not terminate TLS),
 * so it must follow the scheme the browser actually used - not the build mode.
 * Behind a TLS-terminating proxy the app sees http:, so X-Forwarded-Proto is
 * honoured too. The session cookie in middleware.ts and login.astro already
 * works this way (#524).
 */
export function isSecureRequest(url: URL, request: Request): boolean {
  const forwarded = request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim().toLowerCase();
  if (forwarded) return forwarded === "https";
  return url.protocol === "https:";
}

/**
 * Browser-visible origin for links another service must call back into.
 *
 * Astro's server-side URL can point at the internal frontend listener
 * (for example localhost inside Docker). Prefer the original proxy host,
 * then the request Host header, while reusing the forwarded-proto handling
 * above so copied URLs keep the scheme the browser actually used. #534
 */
export function requestOrigin(url: URL, request: Request): string {
  const forwardedHost = request.headers.get("x-forwarded-host")?.split(",")[0]?.trim();
  const host = forwardedHost || request.headers.get("host")?.trim();
  if (!host) return url.origin;
  return `${isSecureRequest(url, request) ? "https" : "http"}://${host}`;
}

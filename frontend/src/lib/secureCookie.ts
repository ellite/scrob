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

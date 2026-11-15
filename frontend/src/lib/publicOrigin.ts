/**
 * The origin (scheme + host) the browser used to reach this page.
 *
 * Astro's Node adapter only trusts the Host header when `security.allowedDomains`
 * is configured, and otherwise reports every request as `http://localhost:<port>`.
 * That is right for security decisions but wrong for a URL we show the user to
 * paste into another service (Radarr/Sonarr import lists, #534): inside the arr's
 * container `localhost` is the arr itself. The header is only echoed back to the
 * person who sent it, so honouring it here is safe. X-Forwarded-* wins so a
 * reverse proxy's public name is shown rather than the internal one.
 */
const HOST_RE = /^[A-Za-z0-9.-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$/;

function first(value: string | null): string {
  return value?.split(",")[0]?.trim() ?? "";
}

export function publicOrigin(url: URL, request: Request): string {
  const headers = request.headers;
  let host = first(headers.get("x-forwarded-host")) || first(headers.get("host"));
  if (!HOST_RE.test(host)) return url.origin;
  const forwardedPort = first(headers.get("x-forwarded-port"));
  if (forwardedPort && /^\d{1,5}$/.test(forwardedPort) && !/:\d+$|\]$/.test(host)) {
    host = `${host}:${forwardedPort}`;
  }
  const proto = first(headers.get("x-forwarded-proto")).toLowerCase();
  const scheme = proto === "https" || proto === "http" ? proto : url.protocol.replace(":", "");
  return `${scheme}://${host}`;
}

import type { AstroCookies } from "astro";

// The SSR server runs in the container's timezone (UTC by default), not the
// viewer's, so formatting a stored UTC timestamp on the server shows the wrong
// clock time (#411). Base.astro writes the browser's IANA zone into this cookie
// and pages pass it as `timeZone` to toLocale*String.
export const VIEWER_TZ_COOKIE = "viewer_tz";

/** The viewer's IANA timezone from the cookie, or undefined (server default)
 *  when it is missing or not a zone this runtime knows. */
export function getViewerTimeZone(cookies: AstroCookies): string | undefined {
  const tz = cookies.get(VIEWER_TZ_COOKIE)?.value;
  if (!tz) return undefined;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    return tz;
  } catch {
    return undefined;
  }
}

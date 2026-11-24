export function calendarSubscriptionUrl(origin, key, days, sonarrOnly, sonarrMonitored) {
  const url = new URL("/api/proxy/calendar/feed.ics", origin);
  url.searchParams.set("api_key", key);
  url.searchParams.set("days", String(days));
  if (sonarrOnly) url.searchParams.set("sonarr_only", "true");
  if (sonarrMonitored) url.searchParams.set("sonarr_monitored", "true");
  return url.toString();
}

import assert from "node:assert/strict";
import test from "node:test";
import { calendarSubscriptionUrl } from "../src/lib/calendar-subscription.js";

test("subscription URL carries the encoded key, window and Sonarr filters", () => {
  const url = new URL(calendarSubscriptionUrl("https://scrob.example", "key&value", 30, true, false));
  assert.equal(url.pathname, "/api/proxy/calendar/feed.ics");
  assert.equal(url.searchParams.get("api_key"), "key&value");
  assert.equal(url.searchParams.get("days"), "30");
  assert.equal(url.searchParams.get("sonarr_only"), "true");
  assert.equal(url.searchParams.has("sonarr_monitored"), false);
});

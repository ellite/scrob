// One readable line per sync-job warning for the Diagnostics page, instead of
// raw JSON. Episode warnings collapse per series: a push against a partial
// library can produce thousands of them. The shapes are the warning dicts
// written by backend/routers/sync.py; unknown ones stay JSON.
//
// The texts come in as message functions (the page passes Paraglide's `m`), so
// this file stays free of the generated output and can be tested on its own.

type Warning = Record<string, any>;
type Message<P = void> = (params: P) => string;

export type WarningMessages = {
  conn_warn_episode_count: Message<{ count: number }>;
  diag_warn_untitled: Message;
  diag_warn_failed: Message;
  conn_push_action_watched: Message;
  conn_push_action_rating: Message;
  conn_push_action_season_rating: Message;
  diag_warn_season_not_on_tmdb: Message<{ show: string; season: number; episodes: string }>;
  diag_warn_not_on_server: Message<{ head: string }>;
  diag_warn_push_failed: Message<{ head: string; reason: string }>;
  diag_warn_push_failed_action: Message<{ head: string; action: string; reason: string }>;
  diag_warn_reason_line: Message<{ head: string; reason: string }>;
  diag_warn_matched: Message<{ head: string }>;
  diag_warn_matched_to: Message<{ head: string; show: string }>;
  diag_warn_matched_to_tvdb: Message<{ head: string; show: string; tvdb: number }>;
  diag_warn_matched_via_tvdb: Message<{ head: string; tvdb: number }>;
  diag_warn_group_not_on_server: Message<{ series: string; episodes: string }>;
  diag_warn_group: Message<{ series: string; episodes: string; reason: string }>;
  diag_warn_group_action: Message<{ series: string; episodes: string; action: string; reason: string }>;
};

type Group = { series: string; count: number; reason: string; action: string; notFound: boolean };

const isPushWarning = (w: Warning) => w.type === "watched_lookup_failed" || w.type === "push_failed";

// Episodes collapse into one line per series + action + reason; one line per
// episode would make the list unusable.
function groupEpisodes(warnings: Warning[], t: WarningMessages): { groups: Group[]; standalone: Warning[] } {
  const grouped = new Map<string, Group>();
  const standalone: Warning[] = [];
  for (const w of warnings) {
    if (w.media_type === "episode" && w.series_name) {
      const action = typeof w.action === "string" ? actionLabel(w.action, t) : "";
      const notFound = w.type === "watched_lookup_failed";
      const reason = typeof w.reason === "string" ? w.reason : "";
      const key = [w.series_name, action, reason, notFound].join("\u0000");
      const existing = grouped.get(key);
      if (existing) existing.count++;
      else grouped.set(key, { series: w.series_name, count: 1, reason, action, notFound });
    } else {
      standalone.push(w);
    }
  }
  return { groups: [...grouped.values()], standalone };
}

function actionLabel(action: string, t: WarningMessages): string {
  if (action.startsWith("season")) return t.conn_push_action_season_rating();
  if (action === "watched") return t.conn_push_action_watched();
  if (action === "rating") return t.conn_push_action_rating();
  return action;
}

function head(w: Warning, t: WarningMessages): string {
  const title = typeof w.title === "string" && w.title ? w.title : t.diag_warn_untitled();
  return w.media_type ? `${title} (${w.media_type})` : title;
}

function describePush(w: Warning, t: WarningMessages): string {
  if (w.type === "watched_lookup_failed") return t.diag_warn_not_on_server({ head: head(w, t) });
  const reason = typeof w.reason === "string" && w.reason ? w.reason : t.diag_warn_failed();
  if (typeof w.action === "string" && w.action) {
    return t.diag_warn_push_failed_action({ head: head(w, t), action: actionLabel(w.action, t), reason });
  }
  return t.diag_warn_push_failed({ head: head(w, t), reason });
}

function describeOne(w: Warning, t: WarningMessages): string {
  if (w.show !== undefined && w.season !== undefined) {
    return t.diag_warn_season_not_on_tmdb({
      show: w.show,
      season: w.season,
      episodes: t.conn_warn_episode_count({ count: w.affected_episodes ?? 0 }),
    });
  }
  if (typeof w.title === "string" && typeof w.reason === "string") {
    const h = head(w, t);
    if (w.matched) {
      if (w.matched_show_title && w.matched_tvdb_id) {
        return t.diag_warn_matched_to_tvdb({ head: h, show: w.matched_show_title, tvdb: w.matched_tvdb_id });
      }
      if (w.matched_show_title) return t.diag_warn_matched_to({ head: h, show: w.matched_show_title });
      if (w.matched_tvdb_id) return t.diag_warn_matched_via_tvdb({ head: h, tvdb: w.matched_tvdb_id });
      return t.diag_warn_matched({ head: h });
    }
    return t.diag_warn_reason_line({ head: h, reason: w.reason });
  }
  return JSON.stringify(w);
}

function describeGroup(g: Group, t: WarningMessages): string {
  const episodes = t.conn_warn_episode_count({ count: g.count });
  if (g.notFound && !g.reason && !g.action) return t.diag_warn_group_not_on_server({ series: g.series, episodes });
  const reason = g.reason || t.diag_warn_failed();
  return g.action
    ? t.diag_warn_group_action({ series: g.series, episodes, action: g.action, reason })
    : t.diag_warn_group({ series: g.series, episodes, reason });
}

export function describeSyncWarnings(warnings: unknown[], t: WarningMessages): string[] {
  const lines: string[] = [];
  const push: Warning[] = [];
  const unmatchedEpisodes: Warning[] = [];
  for (const w of warnings) {
    if (typeof w === "string") lines.push(w);
    else if (w && typeof w === "object" && isPushWarning(w as Warning)) push.push(w as Warning);
    else if (w && typeof w === "object" && (w as Warning).media_type === "episode" && (w as Warning).series_name && !(w as Warning).matched) {
      unmatchedEpisodes.push(w as Warning);
    } else if (w && typeof w === "object") lines.push(describeOne(w as Warning, t));
    else lines.push(JSON.stringify(w));
  }

  const { groups, standalone } = groupEpisodes(push, t);
  for (const g of groups) lines.push(describeGroup(g, t));
  for (const w of standalone) lines.push(describePush(w, t));
  for (const g of groupEpisodes(unmatchedEpisodes, t).groups) lines.push(describeGroup(g, t));
  return lines;
}

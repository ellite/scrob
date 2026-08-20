/** Statuses returned only by the personal Next Up endpoint. The server decides
 * the meaning from episode metadata so every Next Up surface stays consistent. */
export const NEXT_UP_STATUSES = [
  "season_finale",
  "season_premiere",
  "new_today",
  "next_episode",
] as const;

export type NextUpStatus = (typeof NEXT_UP_STATUSES)[number];

export function nextUpBadge(status: NextUpStatus) {
  switch (status) {
    case "season_finale":
      return { label: "Season Finale", className: "border-amber-400/30 bg-amber-500/90 text-amber-950" };
    case "season_premiere":
      return { label: "Season Premiere", className: "border-violet-300/30 bg-violet-600/90 text-white" };
    case "new_today":
      return { label: "New Today", className: "border-emerald-300/30 bg-emerald-600/90 text-white" };
    case "next_episode":
      return { label: "Next Episode", className: "border-blue-300/30 bg-blue-600/90 text-white" };
  }
}

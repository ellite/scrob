// Lokal patch (Popcornmeter). Läser backendens sparade betyg direkt från disk
// när sidan renderas, så att 🍿 syns direkt utan att ★ blinkar förbi.
import fs from "node:fs";

const FILE = process.env.EXT_RATINGS_FILE ?? "/app/backend/data/ext_ratings.json";
let mtime = -1;
let map: Record<string, number> = {};

export function popcornMap(): Record<string, number> {
  try {
    const st = fs.statSync(FILE);
    if (st.mtimeMs !== mtime) {
      const raw = JSON.parse(fs.readFileSync(FILE, "utf8")) as Record<string, [number, { popcorn?: unknown }]>;
      const next: Record<string, number> = {};
      for (const [k, v] of Object.entries(raw)) {
        const p = Number(v?.[1]?.popcorn);
        if (v?.[1]?.popcorn != null && Number.isFinite(p)) next[k] = p;
      }
      map = next;
      mtime = st.mtimeMs;
    }
  } catch {
    /* filen finns inte än */
  }
  return map;
}

export function popcornFor(kind: string | null, id: number | string | null | undefined): number | null {
  if (!kind || id == null || id === "") return null;
  return popcornMap()[`${kind}/${id}`] ?? null;
}

/** Färg och ikon. 90 %+ = 🔥 i cyan. */
export function popcornLook(p: number): { icon: string; cls: string } {
  if (p >= 90) return { icon: "🔥", cls: "text-cyan-400" };
  if (p >= 75) return { icon: "🍿", cls: "text-green-400" };
  if (p >= 60) return { icon: "🍿", cls: "text-yellow-400" };
  return { icon: "🍿", cls: "text-red-400" };
}

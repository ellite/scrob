// Lokal patch (Popcornmeter). Läser backendens sparade betyg direkt från disk
// när sidan renderas, så att 🍿 syns direkt utan att ★ blinkar förbi.
import fs from "node:fs";

const FILE = process.env.EXT_RATINGS_FILE ?? "/app/backend/data/ext_ratings.json";
let mtime = -1;
let map: Record<string, number> = {};
let hot = new Set<string>();

export function popcornMap(): Record<string, number> {
  try {
    const st = fs.statSync(FILE);
    if (st.mtimeMs !== mtime) {
      const raw = JSON.parse(fs.readFileSync(FILE, "utf8")) as Record<string, [number, { popcorn?: unknown; hot?: unknown }]>;
      const next: Record<string, number> = {};
      const nextHot = new Set<string>();
      for (const [k, v] of Object.entries(raw)) {
        const p = Number(v?.[1]?.popcorn);
        if (v?.[1]?.popcorn != null && Number.isFinite(p)) next[k] = p;
        if (v?.[1]?.hot === true) nextHot.add(k);
      }
      map = next;
      hot = nextHot;
      mtime = st.mtimeMs;
    }
  } catch {
    /* filen finns inte än */
  }
  return map;
}

/** Nycklar ("movie/123") som räknas som Verified Hot (uppskattat i backend). */
export function popcornHotKeys(): string[] {
  popcornMap();
  return [...hot];
}

export function popcornHot(kind: string | null, id: number | string | null | undefined): boolean {
  if (!kind || id == null || id === "") return false;
  popcornMap();
  return hot.has(`${kind}/${id}`);
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

/** Egen ikon för (uppskattat) Verified Hot: biografblå sköld med popcornhink. */
export const HOT_SVG = `<svg width="100%" height="100%" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-label="Verified Hot"><defs><linearGradient id="vh-bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1e3a8a"/><stop offset="1" stop-color="#0f172a"/></linearGradient></defs><path d="M12 1.5 21 5v6.2c0 5.4-3.8 9.6-9 11.3-5.2-1.7-9-5.9-9-11.3V5Z" fill="url(#vh-bg)" stroke="#fbbf24" stroke-width="1.1"/><g transform="translate(12 11.4) scale(.92) translate(-11.85 -11.6)"><circle cx="8.70" cy="9.7" r="1.55" fill="#fef9c3"/><circle cx="8.23" cy="9.23" r="0.70" fill="#fff" opacity=".55"/><circle cx="9.24" cy="10.24" r="0.54" fill="#f59e0b" opacity=".5"/><circle cx="10.60" cy="8.5" r="1.75" fill="#fef9c3"/><circle cx="10.07" cy="7.97" r="0.79" fill="#fff" opacity=".55"/><circle cx="11.21" cy="9.11" r="0.61" fill="#f59e0b" opacity=".5"/><circle cx="12.80" cy="8.2" r="1.85" fill="#fef9c3"/><circle cx="12.25" cy="7.64" r="0.83" fill="#fff" opacity=".55"/><circle cx="13.45" cy="8.85" r="0.65" fill="#f59e0b" opacity=".5"/><circle cx="14.90" cy="9.3" r="1.6" fill="#fef9c3"/><circle cx="14.42" cy="8.82" r="0.72" fill="#fff" opacity=".55"/><circle cx="15.46" cy="9.86" r="0.56" fill="#f59e0b" opacity=".5"/><circle cx="11.70" cy="6.5" r="1.45" fill="#fef9c3"/><circle cx="11.26" cy="6.07" r="0.65" fill="#fff" opacity=".55"/><circle cx="12.21" cy="7.01" r="0.51" fill="#f59e0b" opacity=".5"/><circle cx="13.90" cy="6.9" r="1.35" fill="#fef9c3"/><circle cx="13.50" cy="6.50" r="0.61" fill="#fff" opacity=".55"/><circle cx="14.37" cy="7.37" r="0.47" fill="#f59e0b" opacity=".5"/><circle cx="9.70" cy="7.1" r="1.2" fill="#fef9c3"/><circle cx="9.34" cy="6.74" r="0.54" fill="#fff" opacity=".55"/><circle cx="10.12" cy="7.52" r="0.42" fill="#f59e0b" opacity=".5"/><path d="M7.6 10.3h8.8l-1.1 7.8H8.7Z" fill="#fafafa"/><path d="M9.6 10.3h1.4l-.2 7.8H9.8ZM12.9 10.3h1.4l-.6 7.8h-1.2Z" fill="#dc2626"/><path d="M7.6 10.3h8.8" stroke="#dc2626" stroke-width=".7"/></g><circle cx="17.6" cy="17.4" r="4.2" fill="#fbbf24"/><path d="m15.6 17.4 1.4 1.4 2.6-2.8" fill="none" stroke="#0f172a" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

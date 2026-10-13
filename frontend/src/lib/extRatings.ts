// Lokal patch (Popcornmeter). Läser backendens sparade betyg direkt från disk
// när sidan renderas, så att 🍿 syns direkt utan att ★ blinkar förbi.
import fs from "node:fs";

const FILE = process.env.EXT_RATINGS_FILE ?? "/app/backend/data/ext_ratings.json";
let mtime = -1;

// Ikonerna ligger på NAS:en (DATA_DIR/ext-icons), inte i den publika forken.
const ICON_DIR = process.env.EXT_ICON_DIR ?? "/app/backend/data/ext-icons";
const ICON_FILES = { fresh: ["fresh.svg"], rotten: ["rotten.svg"], noScore: ["no-score.svg"], banner: ["ikon-med-banner.svg", "ikon-med-banner-250.svg"] } as const;
export type IconName = keyof typeof ICON_FILES;
const iconCache: Record<string, { mtime: number; svg: string }> = {};
const FALLBACK: Record<IconName, string> = { fresh: "🍅", rotten: "🟢", noScore: "·", banner: "🍅" };

function readIcon(name: IconName): string {
  const file = ICON_FILES[name].map((f) => `${ICON_DIR}/${f}`).find((f) => fs.existsSync(f));
  if (!file) return "";
  try {
    const st = fs.statSync(file);
    const hit = iconCache[name];
    if (hit && hit.mtime === st.mtimeMs) return hit.svg;
    const svg = fs.readFileSync(file, "utf8")
      .replace(/<\?xml[^>]*>/, "")
      .replace(/<svg\b([^>]*?)\swidth="[^"]*"/, "<svg$1")
      .replace(/<svg\b([^>]*?)\sheight="[^"]*"/, "<svg$1")
      .replace(/<svg\b/, '<svg width="100%" height="100%" aria-hidden="true"')
      .trim();
    iconCache[name] = { mtime: st.mtimeMs, svg };
    return svg;
  } catch {
    return "";
  }
}

/** Alla fyra ikonerna som SVG-text (tom sträng om filen saknas). */
export function ratingIcons(): Record<IconName, string> {
  return { fresh: readIcon("fresh"), rotten: readIcon("rotten"), noScore: readIcon("noScore"), banner: readIcon("banner") };
}

/** 90 % eller mer och minst 500 röster. */
export function isBig(p: unknown, votes: unknown): boolean {
  return Number(p) >= 90 && Number(votes) >= 500;
}

export function iconFor(p: number | null, big: boolean): IconName {
  if (p == null) return "noScore";
  if (big && p >= 90) return "banner";
  return p >= 60 ? "fresh" : "rotten";
}

/** HTML för ikonen i given storlek, t.ex. "w-4 h-4". */
export function ratingIconHtml(p: number | null, big: boolean, size: string): string {
  const name = iconFor(p, big);
  const svg = readIcon(name);
  return svg ? `<span class="${size} inline-flex shrink-0">${svg}</span>` : FALLBACK[name];
}

let map: Record<string, number> = {};
let big = new Set<string>();

export function popcornMap(): Record<string, number> {
  try {
    const st = fs.statSync(FILE);
    if (st.mtimeMs !== mtime) {
      const raw = JSON.parse(fs.readFileSync(FILE, "utf8")) as Record<string, [number, { popcorn?: unknown; hot?: unknown; votes?: unknown }]>;
      const next: Record<string, number> = {};
      const nextBig = new Set<string>();
      for (const [k, v] of Object.entries(raw)) {
        const p = Number(v?.[1]?.popcorn);
        if (v?.[1]?.popcorn != null && Number.isFinite(p)) next[k] = p;
        if (isBig(p, v?.[1]?.votes)) nextBig.add(k);
      }
      map = next;
      big = nextBig;
      mtime = st.mtimeMs;
    }
  } catch {
    /* filen finns inte än */
  }
  return map;
}

export function popcornBigKeys(): string[] {
  popcornMap();
  return [...big];
}

export function popcornBig(kind: string | null, id: number | string | null | undefined): boolean {
  if (!kind || id == null || id === "") return false;
  popcornMap();
  return big.has(`${kind}/${id}`);
}

export function popcornFor(kind: string | null, id: number | string | null | undefined): number | null {
  if (!kind || id == null || id === "") return null;
  return popcornMap()[`${kind}/${id}`] ?? null;
}

/** Textfärg för procenten: samma mjukvita på alla nivåer (ikonen visar nivån). */
export function popcornLook(_p: number): { cls: string } {
  return { cls: "text-zinc-100" };
}


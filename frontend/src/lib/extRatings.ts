// Lokal patch (Popcornmeter). Läser backendens sparade betyg direkt från disk
// när sidan renderas, så att 🍿 syns direkt utan att ★ blinkar förbi.
import fs from "node:fs";

const FILE = process.env.EXT_RATINGS_FILE ?? "/app/backend/data/ext_ratings.json";
let mtime = -1;

// Ikonerna ligger på NAS:en (DATA_DIR/ext-icons), inte i den publika forken.
const ICON_DIR = process.env.EXT_ICON_DIR ?? "/app/backend/data/ext-icons";
const ICON_FILES = { fresh: ["fresh.svg"], rotten: ["rotten.svg"], noScore: ["no-score.svg"], banner: ["ikon-med-banner.svg", "ikon-med-banner-250.svg"] } as const;
export type IconName = keyof typeof ICON_FILES;
const iconCache: Record<string, { mtime: number; symbol: string }> = {};
const FALLBACK: Record<IconName, string> = { fresh: "🍅", rotten: "🟢", noScore: "·", banner: "🍅" };
const ICON_NAMES: IconName[] = ["fresh", "rotten", "noScore", "banner"];

function iconFile(name: IconName): string | undefined {
  return ICON_FILES[name].map((f) => `${ICON_DIR}/${f}`).find((f) => fs.existsSync(f));
}

/** Gör om en SVG-fil till <symbol id="ext-icon-NAMN">, så att den bara behöver finnas en gång per sida. */
function readSymbol(name: IconName): { mtime: number; symbol: string } | null {
  const file = iconFile(name);
  if (!file) return null;
  try {
    const st = fs.statSync(file);
    const hit = iconCache[name];
    if (hit && hit.mtime === st.mtimeMs) return hit;
    const src = fs.readFileSync(file, "utf8");
    const open = src.match(/<svg\b[^>]*>/);
    const close = src.lastIndexOf("</svg>");
    if (!open || close < 0) return null;
    const attrs = open[0];
    let viewBox = attrs.match(/viewBox="([^"]+)"/)?.[1];
    if (!viewBox) {
      const w = attrs.match(/\swidth="([\d.]+)/)?.[1];
      const h = attrs.match(/\sheight="([\d.]+)/)?.[1];
      viewBox = w && h ? `0 0 ${w} ${h}` : "0 0 24 24";
    }
    const inner = src.slice((open.index ?? 0) + attrs.length, close)
      .replace(/<title\b[^>]*>[\s\S]*?<\/title>/g, "")
      .replace(/<desc\b[^>]*>[\s\S]*?<\/desc>/g, "")
      .trim();
    const entry = { mtime: st.mtimeMs, symbol: `<symbol id="ext-icon-${name}" viewBox="${viewBox}">${inner}</symbol>` };
    iconCache[name] = entry;
    return entry;
  } catch {
    return null;
  }
}

/** Vilka ikonfiler som finns. */
export function iconsAvailable(): Record<IconName, boolean> {
  return Object.fromEntries(ICON_NAMES.map((n) => [n, !!readSymbol(n)])) as Record<IconName, boolean>;
}

/** En dold <svg> med alla ikoner som symboler. Läggs in en gång per sida. */
export function iconSprite(): string {
  const symbols = ICON_NAMES.map((n) => readSymbol(n)?.symbol ?? "").join("");
  return `<svg id="ext-icon-sprite" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden">${symbols}</svg>`;
}

/** Ändras när en ikonfil ändras (för ETag). */
export function iconsVersion(): string {
  return ICON_NAMES.map((n) => readSymbol(n)?.mtime ?? 0).join("-");
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

/** HTML för ikonen i given storlek, t.ex. "w-4 h-4". Pekar på symbolen i sidans sprite. */
export function ratingIconHtml(p: number | null, big: boolean, size: string): string {
  const name = iconFor(p, big);
  return readSymbol(name)
    ? `<svg class="${size} shrink-0" aria-hidden="true"><use href="#ext-icon-${name}"></use></svg>`
    : FALLBACK[name];
}

let map: Record<string, number> = {};
let big = new Set<string>();
let fetchedAt: Record<string, number> = {};
const FRESH_S = 24 * 3600;

/** Ändras när betygsfilen skrivs (för ETag). */
export function ratingsVersion(): number {
  popcornMap();
  return mtime;
}

export function popcornMap(): Record<string, number> {
  try {
    const st = fs.statSync(FILE);
    if (st.mtimeMs !== mtime) {
      const raw = JSON.parse(fs.readFileSync(FILE, "utf8")) as Record<string, [number, { popcorn?: unknown; votes?: unknown; v?: unknown }]>;
      const next: Record<string, number> = {};
      const nextBig = new Set<string>();
      const nextFetched: Record<string, number> = {};
      for (const [k, v] of Object.entries(raw)) {
        const p = Number(v?.[1]?.popcorn);
        if (v?.[1]?.popcorn != null && Number.isFinite(p)) next[k] = p;
        if (isBig(p, v?.[1]?.votes)) nextBig.add(k);
        // Äldre poster (utan röstantal) räknas som gamla direkt.
        nextFetched[k] = v?.[1]?.v === 2 ? Number(v?.[0]) || 0 : 0;
      }
      map = next;
      big = nextBig;
      fetchedAt = nextFetched;
      mtime = st.mtimeMs;
    }
  } catch {
    /* filen finns inte än */
  }
  return map;
}

/** Betyg som är äldre än ett dygn (eller saknar röstantal): visas direkt men hämtas om i bakgrunden. */
export function popcornStaleKeys(): string[] {
  popcornMap();
  const now = Date.now() / 1000;
  return Object.keys(map).filter((k) => now - (fetchedAt[k] ?? 0) >= FRESH_S);
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


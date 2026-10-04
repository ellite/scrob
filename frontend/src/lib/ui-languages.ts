import { locales } from "../paraglide/runtime.js";

// Each UI language labelled in its own language (an endonym, e.g. "Français"),
// so a user can find theirs whatever language the page is currently in. Derived
// from Intl rather than listed by hand: adding a locale to
// project.inlang/settings.json is enough for it to appear in the pickers.
function endonym(code: string): string {
  const name = new Intl.DisplayNames([code], { type: "language" }).of(code) ?? code;
  return name.charAt(0).toLocaleUpperCase(code) + name.slice(1);
}

export const UI_LANGUAGES = locales.map(code => ({ code, name: endonym(code) }));

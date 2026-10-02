// Bridges the account's UI language preference (user_settings.ui_language) into
// Paraglide's locale resolution.
//
// Paraglide resolves a locale from the incoming Request alone (cookie, then
// Accept-Language), but the preference we want to win over both lives in
// context.locals.settings, which the middleware fetches per request. A custom
// server strategy is the supported way to feed it in: extractLocaleFromRequestAsync
// runs every custom strategy first, with the original Request, before falling
// back to the built-in ones - so "account preference, else cookie, else browser"
// falls out of the ordering for free.
//
// The preference is keyed on the Request object rather than kept in a module
// variable: two requests are in flight at once under any real load, and a shared
// variable would hand one user's language to another. The map is weak, so an
// entry disappears with the request that owns it.
import { defineCustomServerStrategy, type Locale } from "../paraglide/runtime.js";

export const ACCOUNT_STRATEGY = "custom-account";

const accountLocales = new WeakMap<Request, Locale>();

/** Called by the middleware once the signed-in user's settings are known. */
export function setAccountLocale(request: Request, locale: Locale): void {
  accountLocales.set(request, locale);
}

defineCustomServerStrategy(ACCOUNT_STRATEGY, {
  // Undefined (anonymous visitor, or no preference saved) falls through to the
  // next strategy, which is the ui_language cookie.
  getLocale: (request?: Request) => (request ? accountLocales.get(request) : undefined),
});

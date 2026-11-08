import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("requestOrigin prefers forwarded/public request hosts over Astro's internal origin", async () => {
  const source = await readFile(path.join(frontendRoot, "src", "lib", "secureCookie.ts"), "utf8");
  assert.match(source, /export function requestOrigin/);
  assert.match(source, /headers\.get\("x-forwarded-host"\)/);
  assert.match(source, /headers\.get\("host"\)/);
  assert.match(source, /isSecureRequest\(url, request\)/);
  assert.match(source, /if \(!host\) return url\.origin/);
});

test("list ARR import URLs use the proxy-aware request origin", async () => {
  const source = await readFile(path.join(frontendRoot, "src", "pages", "list", "[id].astro"), "utf8");
  assert.match(source, /import \{ requestOrigin \} from "\.\.\/\.\.\/lib\/secureCookie"/);
  assert.match(source, /const origin = requestOrigin\(Astro\.url, Astro\.request\)/);
  assert.match(source, /\$\{origin\}\/api\/proxy\/radarr-compat/);
  assert.match(source, /\$\{origin\}\/api\/proxy\/sonarr-compat/);
  assert.doesNotMatch(source, /const origin = Astro\.url\.origin/);
});

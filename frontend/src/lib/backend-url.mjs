const DEFAULT_BACKEND_PORT = 7331;

/**
 * Resolve the server-to-server URL used by Astro's SSR/API proxy.
 *
 * `BACKEND_URL` is the preferred runtime setting. `BACKEND_PORT` remains a
 * backwards-compatible shortcut for a backend listening on this host's
 * loopback interface. Keeping the default on loopback means the backend does
 * not become publicly reachable merely because the frontend is deployed.
 */
export function resolveBackendUrl(environment = process.env) {
  const configuredUrl = environment.BACKEND_URL?.trim();
  if (configuredUrl) {
    let parsed;
    try {
      parsed = new URL(configuredUrl);
    } catch {
      throw new Error("BACKEND_URL must be an absolute http:// or https:// URL");
    }

    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      !parsed.hostname ||
      parsed.username ||
      parsed.password ||
      (parsed.pathname !== "/" && parsed.pathname !== "") ||
      parsed.search ||
      parsed.hash
    ) {
      throw new Error(
        "BACKEND_URL must be an http:// or https:// origin without credentials, a path, query, or fragment"
      );
    }
    return parsed.origin;
  }

  const configuredPort = environment.BACKEND_PORT?.trim();
  const port = configuredPort === undefined || configuredPort === "" ? DEFAULT_BACKEND_PORT : Number(configuredPort);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("BACKEND_PORT must be an integer between 1 and 65535");
  }
  return `http://127.0.0.1:${port}`;
}

// Astro/Vite supplies .env values through import.meta.env in development and
// at build time. In a standalone production build, process.env is the runtime
// source of truth so an image/service can be configured without rebuilding.
const buildEnvironment = typeof import.meta.env === "object" ? import.meta.env : {};
export const backendUrl = resolveBackendUrl({ ...buildEnvironment, ...process.env });

export function backendPath(path) {
  return `${backendUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

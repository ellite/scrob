import { readFileSync } from "node:fs";

/** Written into the image at build time, next to the APP_VERSION build arg. */
export const VERSION_FILE = "/app/VERSION";

/**
 * The version this image was built as.
 *
 * Read from a file baked into the image rather than only from the
 * APP_VERSION env var (#140): container updaters such as Dockhand, WUD or
 * drydock recreate the container from the old container's config, which
 * carries the image-baked ENV of the *previous* image along with it, so the
 * env var kept reporting the version from before the update. The file lives
 * in the image layer and always matches the running image. The env var stays
 * the fallback for dev runs and images built without the file.
 */
export function readAppVersion(
  env: Record<string, string | undefined> = process.env,
  file: string = VERSION_FILE,
): string {
  try {
    const fromFile: string = readFileSync(file, "utf8").trim();
    if (fromFile) return fromFile;
  } catch {
    // no version file (dev server, older image) - fall back to the env var
  }
  return env.APP_VERSION?.trim() || "dev";
}

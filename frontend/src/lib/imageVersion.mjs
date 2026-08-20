import { readFile } from "node:fs/promises";

const IMAGE_VERSION_FILE = "/app/APP_VERSION";

/**
 * Read the version baked into the image filesystem. This intentionally does
 * not let APP_VERSION override the baked value: some container updaters carry
 * the previous container's environment into a replacement container. The
 * environment remains a fallback for bare-metal development and deployments.
 */
export async function readImageVersion(readFileFn = readFile, env = process.env) {
  try {
    const bakedVersion = (await readFileFn(IMAGE_VERSION_FILE, "utf8")).trim();
    if (bakedVersion) return bakedVersion;
  } catch {}
  return env.APP_VERSION?.trim() || "dev";
}

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workflowUrl = new URL("../../.github/workflows/docker-x64.yml", import.meta.url);

test("the development workflow cannot overwrite release latest tags", async () => {
  const workflow = await readFile(workflowUrl, "utf8");

  assert.doesNotMatch(workflow, /type=raw,value=latest/);
  assert.equal(
    workflow.match(/APP_VERSION=\$\{\{ steps\.version\.outputs\.value \}\}/g)?.length,
    2,
  );
});

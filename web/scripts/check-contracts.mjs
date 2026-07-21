import { readFile, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { spawnSync } from "node:child_process";

import { generatedDirectory, renderContracts } from "./generate-contracts.mjs";

const repositoryRoot = fileURLToPath(new URL("../..", import.meta.url));
const schemaCheck = spawnSync("uv", ["run", "python", "-m", "gameforge.apps.api.schema", "--check"], {
  cwd: repositoryRoot,
  stdio: "inherit",
});
if (schemaCheck.status !== 0) {
  process.exit(schemaCheck.status ?? 1);
}

const expected = await renderContracts();
const actualFiles = (await readdir(generatedDirectory)).filter((name) => name.endsWith(".ts")).sort();
const expectedFiles = [...expected.keys()].sort();
const drift = [];

if (JSON.stringify(actualFiles) !== JSON.stringify(expectedFiles)) {
  drift.push(
    `generated file set differs: expected ${expectedFiles.join(", ")}; received ${actualFiles.join(", ")}`,
  );
}

for (const [filename, expectedSource] of expected) {
  const actualSource = await readFile(path.join(generatedDirectory, filename), "utf8").catch(() => null);
  if (actualSource !== expectedSource) {
    drift.push(filename);
  }
}

if (drift.length > 0) {
  console.error(`Generated API contracts are stale:\n${drift.map((item) => `- ${item}`).join("\n")}`);
  console.error("Run npm run contracts:generate and commit the result.");
  process.exitCode = 1;
} else {
  console.log("Generated API contracts match the committed schemas.");
}

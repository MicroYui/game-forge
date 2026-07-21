import { readFileSync } from "node:fs";

const manifest = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
const expectedNode = manifest.engines?.node;
const expectedNpm = manifest.engines?.npm;
const actualNode = process.version.replace(/^v/, "");
const actualNpm = process.env.npm_config_user_agent?.match(/^npm\/([^ ]+)/)?.[1];

const failures = [];
if (actualNode !== expectedNode) {
  failures.push(`Node ${expectedNode} is required; received ${actualNode}.`);
}
if (actualNpm !== expectedNpm) {
  failures.push(`npm ${expectedNpm} is required; received ${actualNpm ?? "unknown"}.`);
}

for (const [sectionName, dependencies] of Object.entries({
  dependencies: manifest.dependencies,
  devDependencies: manifest.devDependencies,
})) {
  for (const [name, version] of Object.entries(dependencies ?? {})) {
    if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(version)) {
      failures.push(`${sectionName}.${name} must use an exact version; received ${version}.`);
    }
  }
}

if (failures.length > 0) {
  console.error(failures.join("\n"));
  process.exitCode = 1;
} else {
  console.log(`GameForge Web toolchain verified: Node ${actualNode}, npm ${actualNpm}.`);
}

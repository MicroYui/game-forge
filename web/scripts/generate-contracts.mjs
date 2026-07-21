import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { compile } from "json-schema-to-typescript";
import openapiTS, { astToString } from "openapi-typescript";

const repositoryRoot = fileURLToPath(new URL("../..", import.meta.url));
const generatedDirectory = path.join(repositoryRoot, "web/src/api/generated");
const openApiPath = path.join(repositoryRoot, "docs/api/openapi-v1.json");
const schemaDirectory = path.join(repositoryRoot, "docs/api/schemas");

const generatedHeader = "/** Generated from the committed GameForge API contracts. Do not edit by hand. */\n";

const schemaFiles = ["sse-run-event-v1.json", "ws-client-command-v1.json", "ws-server-frame-v1.json"];

function operationIds(document) {
  return Object.values(document.paths).flatMap((pathItem) =>
    Object.values(pathItem).flatMap((operation) =>
      operation && typeof operation === "object" && "operationId" in operation ? [operation.operationId] : [],
    ),
  );
}

export async function renderContracts() {
  const openApiDocument = JSON.parse(await readFile(openApiPath, "utf8"));
  const openApiSource = generatedHeader + astToString(await openapiTS(openApiDocument));
  const ids = operationIds(openApiDocument);

  if (ids.some((id) => typeof id !== "string") || new Set(ids).size !== ids.length) {
    throw new Error("OpenAPI operationId values must be present and unique.");
  }
  for (const id of ids) {
    if (!openApiSource.includes(`${id}: {`)) {
      throw new Error(`Generated OpenAPI types omitted operationId ${id}.`);
    }
  }

  const rendered = new Map([["openapi.ts", openApiSource]]);
  for (const schemaFile of schemaFiles) {
    const schema = JSON.parse(await readFile(path.join(schemaDirectory, schemaFile), "utf8"));
    const outputFile = schemaFile.replace(/\.json$/, ".ts");
    const source = await compile(schema, schema.title, {
      bannerComment: generatedHeader.trimEnd(),
      enableConstEnums: false,
      style: {
        semi: true,
        singleQuote: false,
        tabWidth: 2,
      },
    });
    rendered.set(outputFile, source);
  }

  rendered.set(
    "index.ts",
    `${generatedHeader}export type { components, operations, paths } from "./openapi";\n` +
      `export type { RunEvent } from "./sse-run-event-v1";\n` +
      `export type { RunCommandV1 } from "./ws-client-command-v1";\n` +
      `export type { RunCommandServerFrame } from "./ws-server-frame-v1";\n`,
  );
  return rendered;
}

export async function writeContracts() {
  await mkdir(generatedDirectory, { recursive: true });
  for (const [filename, source] of await renderContracts()) {
    await writeFile(path.join(generatedDirectory, filename), source, "utf8");
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await writeContracts();
  console.log(`Generated API contracts in ${path.relative(repositoryRoot, generatedDirectory)}.`);
}

export { generatedDirectory };

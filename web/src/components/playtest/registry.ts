export type BundledTraceComponentKey = "generic" | "aureus-2d";
export type TraceRendererStatus = "active" | "disabled";

export interface TraceRendererDefinitionV1 {
  capabilities: readonly string[];
  component_key: string;
  environment_contract_versions: readonly string[];
  renderer_id: string;
  status: TraceRendererStatus;
  trace_payload_schema_ids: readonly string[];
  version: number;
}

export interface TraceRendererRegistryV1 {
  definitions: readonly TraceRendererDefinitionV1[];
  registry_digest: string;
  registry_version: number;
}

export interface TraceRendererRequest {
  capabilities: readonly string[];
  environmentContractVersion: string;
  rendererId: string;
  rendererVersion: number;
  tracePayloadSchemaId: string;
}

export interface AureusSpatialPoint {
  x: number;
  y: number;
}

export interface AureusSpatialEntity extends AureusSpatialPoint {
  id: string;
  kind: string;
  label?: string;
}

export interface AureusSpatialFrame {
  entities: readonly AureusSpatialEntity[];
  frame_id: string;
  player: AureusSpatialPoint;
}

export interface AureusSpatial2DPayload {
  frames: readonly AureusSpatialFrame[];
  map: {
    blocked: readonly AureusSpatialPoint[];
    height: number;
    width: number;
  };
  renderer_payload_schema_id: "aureus-spatial-2d@1";
}

export type TraceRendererFallbackReason =
  | "invalid-registry"
  | "unknown"
  | "disabled"
  | "environment-incompatible"
  | "schema-incompatible"
  | "capability-mismatch"
  | "invalid-payload";

export interface TraceRendererResolution {
  componentKey: BundledTraceComponentKey;
  definition: TraceRendererDefinitionV1 | null;
  fallbackReason: TraceRendererFallbackReason | null;
  validatedPayload: AureusSpatial2DPayload | null;
}

const BUNDLED_COMPONENT_KEYS = new Set<BundledTraceComponentKey>(["generic", "aureus-2d"]);

const FROZEN_DEFINITIONS: readonly TraceRendererDefinitionV1[] = [
  {
    renderer_id: "aureus.legacy-2d",
    version: 1,
    status: "disabled",
    environment_contract_versions: ["agent-env@2", "env@1", "generic-agent-env@1"],
    trace_payload_schema_ids: ["playtest-trace@1"],
    capabilities: ["spatial_2d"],
    component_key: "aureus-2d",
  },
  {
    renderer_id: "aureus.spatial-2d",
    version: 1,
    status: "active",
    environment_contract_versions: ["agent-env@2", "env@1", "generic-agent-env@1"],
    trace_payload_schema_ids: ["playtest-trace@1"],
    capabilities: ["spatial_2d"],
    component_key: "aureus-2d",
  },
  {
    renderer_id: "generic.timeline",
    version: 1,
    status: "active",
    environment_contract_versions: ["*"],
    trace_payload_schema_ids: ["*"],
    capabilities: [],
    component_key: "generic",
  },
];

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

const FROZEN_DEFINITIONS_CANONICAL = canonicalJson(FROZEN_DEFINITIONS);
for (const definition of FROZEN_DEFINITIONS) {
  Object.freeze(definition.environment_contract_versions);
  Object.freeze(definition.trace_payload_schema_ids);
  Object.freeze(definition.capabilities);
  Object.freeze(definition);
}
Object.freeze(FROZEN_DEFINITIONS);

// sha256(canonical JSON of {registry_version, definitions}); the static content
// comparison below keeps this synchronous and prevents runtime script/plugin loading.
const FROZEN_REGISTRY_DIGEST = "9087497c0eb0b09af7dca9706c2256d638b640c465a784e108832a4599b36ae3";

export const TRACE_RENDERER_REGISTRY: TraceRendererRegistryV1 = Object.freeze({
  registry_version: 1,
  definitions: FROZEN_DEFINITIONS,
  registry_digest: FROZEN_REGISTRY_DIGEST,
});

type RegistryValidation = { ok: true } | { ok: false; reason: string };

function definitionKey(definition: TraceRendererDefinitionV1): string {
  return `${definition.renderer_id}\u0000${String(definition.version).padStart(12, "0")}`;
}

function stableUnique(values: readonly string[]): boolean {
  return values.every((value, index) => index === 0 || values[index - 1] < value);
}

function frozenContentMatches(registry: TraceRendererRegistryV1): boolean {
  return canonicalJson(registry.definitions) === FROZEN_DEFINITIONS_CANONICAL;
}

export function validateTraceRendererRegistry(registry: TraceRendererRegistryV1): RegistryValidation {
  if (registry.registry_version !== 1) return { ok: false, reason: "registry_version" };

  const keys = registry.definitions.map(definitionKey);
  if (new Set(keys).size !== keys.length) return { ok: false, reason: "unique" };
  if (!keys.every((key, index) => index === 0 || keys[index - 1] < key)) {
    return { ok: false, reason: "sorted" };
  }

  for (const definition of registry.definitions) {
    if (!BUNDLED_COMPONENT_KEYS.has(definition.component_key as BundledTraceComponentKey)) {
      return { ok: false, reason: "component_key" };
    }
    if (
      !stableUnique(definition.environment_contract_versions) ||
      !stableUnique(definition.trace_payload_schema_ids) ||
      !stableUnique(definition.capabilities)
    ) {
      return { ok: false, reason: "definition arrays" };
    }
  }

  if (registry.registry_digest !== FROZEN_REGISTRY_DIGEST || !frozenContentMatches(registry)) {
    return { ok: false, reason: "registry_digest" };
  }
  return { ok: true };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isIntegerIn(value: unknown, minimum: number, maximum: number): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= minimum && value <= maximum;
}

function validPoint(value: unknown, width: number, height: number): value is AureusSpatialPoint {
  return isRecord(value) && isIntegerIn(value.x, 0, width - 1) && isIntegerIn(value.y, 0, height - 1);
}

export function isAureusSpatial2DPayload(value: unknown): value is AureusSpatial2DPayload {
  if (!isRecord(value) || value.renderer_payload_schema_id !== "aureus-spatial-2d@1") return false;
  if (!isRecord(value.map)) return false;
  const { width, height, blocked } = value.map;
  if (!isIntegerIn(width, 1, 256) || !isIntegerIn(height, 1, 256) || !Array.isArray(blocked)) {
    return false;
  }
  if (blocked.length > 4096 || !blocked.every((point) => validPoint(point, width, height))) {
    return false;
  }
  if (!Array.isArray(value.frames) || value.frames.length > 10_000) return false;
  const frameIds = new Set<string>();
  for (const frame of value.frames) {
    if (!isRecord(frame) || typeof frame.frame_id !== "string" || frame.frame_id.length === 0) {
      return false;
    }
    if (frameIds.has(frame.frame_id) || !validPoint(frame.player, width, height)) return false;
    frameIds.add(frame.frame_id);
    if (!Array.isArray(frame.entities) || frame.entities.length > 1024) return false;
    for (const entity of frame.entities) {
      if (
        !isRecord(entity) ||
        typeof entity.id !== "string" ||
        entity.id.length === 0 ||
        typeof entity.kind !== "string" ||
        entity.kind.length === 0 ||
        (entity.label !== undefined && typeof entity.label !== "string") ||
        !validPoint(entity, width, height)
      ) {
        return false;
      }
    }
  }
  return true;
}

function includesCompatible(values: readonly string[], actual: string): boolean {
  return values.includes("*") || values.includes(actual);
}

function genericFallback(
  fallbackReason: TraceRendererFallbackReason,
  definition: TraceRendererDefinitionV1 | null = null,
): TraceRendererResolution {
  return { componentKey: "generic", definition, fallbackReason, validatedPayload: null };
}

export function resolveTraceRenderer(
  request: TraceRendererRequest,
  rendererPayload: unknown,
  registry: TraceRendererRegistryV1 = TRACE_RENDERER_REGISTRY,
): TraceRendererResolution {
  if (!validateTraceRendererRegistry(registry).ok) return genericFallback("invalid-registry");

  const definition = registry.definitions.find(
    (candidate) =>
      candidate.renderer_id === request.rendererId && candidate.version === request.rendererVersion,
  );
  if (!definition) return genericFallback("unknown");
  if (definition.status !== "active") return genericFallback("disabled", definition);
  if (!includesCompatible(definition.environment_contract_versions, request.environmentContractVersion)) {
    return genericFallback("environment-incompatible", definition);
  }
  if (!includesCompatible(definition.trace_payload_schema_ids, request.tracePayloadSchemaId)) {
    return genericFallback("schema-incompatible", definition);
  }
  if (!definition.capabilities.every((capability) => request.capabilities.includes(capability))) {
    return genericFallback("capability-mismatch", definition);
  }
  if (definition.component_key === "aureus-2d") {
    if (!isAureusSpatial2DPayload(rendererPayload)) {
      return genericFallback("invalid-payload", definition);
    }
    return {
      componentKey: "aureus-2d",
      definition,
      fallbackReason: null,
      validatedPayload: rendererPayload,
    };
  }
  return { componentKey: "generic", definition, fallbackReason: null, validatedPayload: null };
}

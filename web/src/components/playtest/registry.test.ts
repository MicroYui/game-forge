import { describe, expect, it } from "vitest";

import {
  TRACE_RENDERER_REGISTRY,
  resolveTraceRenderer,
  validateTraceRendererRegistry,
  type TraceRendererRegistryV1,
} from "./registry";

const genericPayload = {
  trace_id: "trace:fixture",
  frames: [
    {
      action: { kind: "observe" },
      last_action_result: "observed",
      tick: 7,
      state_hash: `sha256:${"1".repeat(64)}`,
    },
  ],
};

const aureusPayload = {
  renderer_payload_schema_id: "aureus-spatial-2d@1",
  map: { width: 8, height: 6, blocked: [{ x: 2, y: 2 }] },
  frames: [
    {
      frame_id: "step:0",
      player: { x: 1, y: 1 },
      entities: [{ id: "npc:lincheng", kind: "npc", x: 4, y: 3, label: "林澄" }],
    },
  ],
};

const compatibleRequest = {
  rendererId: "aureus.spatial-2d",
  rendererVersion: 1,
  environmentContractVersion: "env@1",
  tracePayloadSchemaId: "playtest-trace@1",
  capabilities: ["spatial_2d"],
} as const;

function registryWith(
  update: (registry: TraceRendererRegistryV1) => TraceRendererRegistryV1,
): TraceRendererRegistryV1 {
  return update(structuredClone(TRACE_RENDERER_REGISTRY));
}

describe("TraceRendererRegistryV1", () => {
  it("accepts the frozen, uniquely sorted bundled registry", () => {
    expect(validateTraceRendererRegistry(TRACE_RENDERER_REGISTRY)).toEqual({ ok: true });
    expect(
      TRACE_RENDERER_REGISTRY.definitions.map(({ renderer_id, version }) => [renderer_id, version]),
    ).toEqual([
      ["aureus.legacy-2d", 1],
      ["aureus.spatial-2d", 1],
      ["generic.timeline", 1],
    ]);
  });

  it.each([
    [
      "registry version",
      registryWith((registry) => ({ ...registry, registry_version: 2 })),
      "registry_version",
    ],
    [
      "registry digest",
      registryWith((registry) => ({ ...registry, registry_digest: "0".repeat(64) })),
      "registry_digest",
    ],
    [
      "definition order",
      registryWith((registry) => ({ ...registry, definitions: [...registry.definitions].reverse() })),
      "sorted",
    ],
    [
      "duplicate definition",
      registryWith((registry) => ({
        ...registry,
        definitions: [registry.definitions[0], registry.definitions[0], registry.definitions[2]],
      })),
      "unique",
    ],
    [
      "unbundled component key",
      registryWith((registry) => ({
        ...registry,
        definitions: registry.definitions.map((definition, index) =>
          index === 0 ? { ...definition, component_key: "remote-script" } : definition,
        ),
      })),
      "component_key",
    ],
  ])("rejects a changed %s", (_label, registry, reason) => {
    expect(validateTraceRendererRegistry(registry)).toEqual({ ok: false, reason });
  });
});

describe("resolveTraceRenderer", () => {
  it("selects the bundled Aureus renderer only for a compatible, valid spatial payload", () => {
    expect(resolveTraceRenderer(compatibleRequest, aureusPayload)).toMatchObject({
      componentKey: "aureus-2d",
      definition: { renderer_id: "aureus.spatial-2d", version: 1 },
      fallbackReason: null,
    });
  });

  it.each([
    ["unknown", { ...compatibleRequest, rendererId: "unknown.renderer" }, aureusPayload],
    ["disabled", { ...compatibleRequest, rendererId: "aureus.legacy-2d" }, aureusPayload],
    [
      "environment-incompatible",
      { ...compatibleRequest, environmentContractVersion: "other-env@9" },
      aureusPayload,
    ],
    ["schema-incompatible", { ...compatibleRequest, tracePayloadSchemaId: "other-trace@1" }, aureusPayload],
    ["capability-mismatch", { ...compatibleRequest, capabilities: [] }, aureusPayload],
    ["invalid-payload", compatibleRequest, { ...aureusPayload, map: { ...aureusPayload.map, width: 0 } }],
  ])("falls back to generic inspection for %s", (fallbackReason, request, payload) => {
    expect(resolveTraceRenderer(request, payload)).toMatchObject({
      componentKey: "generic",
      fallbackReason,
    });
  });

  it("does not validate a generic fallback as an Aureus payload", () => {
    expect(
      resolveTraceRenderer({ ...compatibleRequest, rendererId: "unknown.renderer" }, genericPayload),
    ).toMatchObject({ componentKey: "generic", fallbackReason: "unknown" });
  });
});

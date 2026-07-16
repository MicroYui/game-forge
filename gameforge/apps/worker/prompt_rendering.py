"""Canonical, typed-slot, multi-source prompt authority.

Handler messages are never provenance authority.  A retained exact
``(agent_node_id, prompt_version)`` binding owns the immutable template, typed
source slots, their cardinalities and purposes, the primary document-version
projection, and a deterministic renderer.  The renderer receives the complete
stable-ordered verified source collection twice.  Each rendered source message
must name its exact non-empty contributor subset, so trust and provenance never
leak across unrelated prompt parts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import json
from typing import Literal

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import ArtifactV2
from gameforge.contracts.model_router import (
    Message,
    ModelRequestV1,
    ModelRequestV2,
    PrefixCacheDirectiveV1,
    ToolSchemaRef,
    compute_prefix_hash,
)
from gameforge.contracts.provenance import (
    OriginRefV1,
    PromptPartV1,
    PromptPurpose,
    ProvenanceTransformationV1,
    ProvenanceV1,
    SourceKindDefinitionV1,
    SourceKindRegistryV1,
    TrustLevel,
    most_conservative_trust,
)


SourceMessageRole = Literal["user", "assistant", "tool"]
MAX_PROMPT_SOURCE_COUNT = 256
MAX_PROMPT_SOURCE_BYTES = 16 * 1024 * 1024
MAX_PROMPT_TOOL_SCHEMAS = 256
MAX_PROMPT_REQUEST_PARAMS_BYTES = 64 * 1024


def _stable_nonempty(values: tuple[str, ...], *, field: str) -> tuple[str, ...]:
    if (
        not values
        or any(not isinstance(value, str) or not value for value in values)
        or values != tuple(sorted(set(values)))
    ):
        raise ValueError(f"{field} must be stable-unique and non-empty")
    return values


@dataclass(frozen=True, slots=True, order=True)
class CanonicalPromptSourceShapeV1:
    """One exact Artifact-kind/schema/source-kind combination for a slot."""

    artifact_kind: str
    payload_schema_id: str
    source_kind_registry_version: int
    source_kind_id: str

    def __post_init__(self) -> None:
        for name in ("artifact_kind", "payload_schema_id", "source_kind_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"prompt source shape {name} must be non-empty")
        if (
            isinstance(self.source_kind_registry_version, bool)
            or not isinstance(self.source_kind_registry_version, int)
            or self.source_kind_registry_version < 1
        ):
            raise ValueError("prompt source shape registry version must be positive")


@dataclass(frozen=True, slots=True)
class CanonicalPromptSourceSlotV1:
    """A typed source role with exact cardinality bounds and allowed purposes."""

    slot_id: str
    min_count: int
    max_count: int
    max_bytes: int
    allowed_shapes: tuple[CanonicalPromptSourceShapeV1, ...]
    allowed_prompt_purposes: tuple[PromptPurpose, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.slot_id, str) or not self.slot_id:
            raise ValueError("prompt source slot_id must be non-empty")
        if (
            isinstance(self.min_count, bool)
            or not isinstance(self.min_count, int)
            or isinstance(self.max_count, bool)
            or not isinstance(self.max_count, int)
            or self.min_count < 0
            or self.max_count < 1
            or self.max_count > MAX_PROMPT_SOURCE_COUNT
            or self.min_count > self.max_count
        ):
            raise ValueError("prompt source slot cardinality is invalid")
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or self.max_bytes < 1
            or self.max_bytes > MAX_PROMPT_SOURCE_BYTES
        ):
            raise ValueError("prompt source slot max_bytes is outside hard bounds")
        if not self.allowed_shapes or self.allowed_shapes != tuple(
            sorted(set(self.allowed_shapes))
        ):
            raise ValueError("prompt source slot shapes must be stable-unique")
        if not self.allowed_prompt_purposes or self.allowed_prompt_purposes != tuple(
            sorted(set(self.allowed_prompt_purposes))
        ):
            raise ValueError("prompt source slot purposes must be stable-unique")


@dataclass(frozen=True, slots=True)
class CanonicalSourceMessageV1:
    """One message derived from an exact contributor subset of the full source set."""

    role: SourceMessageRole
    text: str
    purpose: PromptPurpose
    source_artifact_ids: tuple[str, ...]
    rendered_source_kind_registry_version: int
    rendered_source_kind_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("canonical source message text must be non-empty")
        if self.purpose == "instruction":
            raise ValueError("source renderer cannot mint instruction purpose")
        if self.role == "tool" and self.purpose != "tool_output":
            raise ValueError("tool messages require tool_output purpose")
        _stable_nonempty(self.source_artifact_ids, field="source_artifact_ids")
        if (
            isinstance(self.rendered_source_kind_registry_version, bool)
            or not isinstance(self.rendered_source_kind_registry_version, int)
            or self.rendered_source_kind_registry_version < 1
        ):
            raise ValueError("rendered source-kind registry version must be positive")
        if not isinstance(self.rendered_source_kind_id, str) or not (self.rendered_source_kind_id):
            raise ValueError("rendered source-kind id must be non-empty")


@dataclass(frozen=True, slots=True)
class RetainedTemplateMessageV1:
    """One immutable system-template part retained by trusted authority."""

    role: Literal["system"]
    part: PromptPartV1

    def __post_init__(self) -> None:
        if self.part.purpose != "instruction":
            raise ValueError("retained system template must have instruction purpose")
        if self.part.provenance.parent_source_artifact_ids:
            raise ValueError("retained system template provenance cannot invent source parents")
        expected = sha256_lowerhex(self.part.text.encode("utf-8"))
        if self.part.provenance.source_hash != expected:
            raise ValueError("retained system template provenance hash differs from text")


@dataclass(frozen=True, slots=True)
class CanonicalPromptSourceV1:
    """One independently verified source passed to the retained renderer."""

    slot_id: str
    artifact: ArtifactV2
    payload: bytes
    provenance: ProvenanceV1
    source_kind: SourceKindDefinitionV1


@dataclass(frozen=True, slots=True)
class _VerifiedPromptSourceMetadata:
    slot_id: str
    artifact: ArtifactV2
    provenance: ProvenanceV1
    source_kind: SourceKindDefinitionV1


SourceRenderer = Callable[
    [tuple[CanonicalPromptSourceV1, ...]],
    tuple[CanonicalSourceMessageV1, ...],
]


def _schema_set_digest(*, tool_version: str, schemas: tuple[ToolSchemaRef, ...]) -> str:
    return sha256_lowerhex(
        canonical_json(
            {
                "tool_version": tool_version,
                "tool_schemas": [schema.model_dump(mode="json") for schema in schemas],
            }
        ).encode("utf-8")
    )


@dataclass(frozen=True, slots=True)
class CanonicalToolSchemaSetV1:
    """Retained exact ``tool_version -> ordered ToolSchemaRef set`` authority."""

    tool_version: str
    tool_schemas: tuple[ToolSchemaRef, ...]
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.tool_version, str) or not self.tool_version:
            raise ValueError("tool schema set tool_version must be non-empty")
        keys = tuple((schema.name, schema.version) for schema in self.tool_schemas)
        if len(keys) > MAX_PROMPT_TOOL_SCHEMAS or keys != tuple(sorted(set(keys))):
            raise ValueError("tool schema refs must be stable-unique")
        if any(not name or not version for name, version in keys):
            raise ValueError("tool schema refs must have non-empty name/version")
        expected = _schema_set_digest(
            tool_version=self.tool_version,
            schemas=self.tool_schemas,
        )
        if self.digest != expected:
            raise ValueError("tool schema set digest differs from exact refs")


@dataclass(frozen=True, slots=True)
class CanonicalToolSchemaSetRefV1:
    tool_version: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.tool_version, str) or not self.tool_version:
            raise ValueError("tool schema set ref tool_version must be non-empty")
        if (
            not isinstance(self.digest, str)
            or len(self.digest) != 64
            or any(character not in "0123456789abcdef" for character in self.digest)
        ):
            raise ValueError("tool schema set ref digest must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class _ResolvedToolSchemaSet:
    tool_version: str
    identities: tuple[tuple[str, str], ...]


def build_tool_schema_set(
    *, tool_version: str, tool_schemas: tuple[ToolSchemaRef, ...]
) -> CanonicalToolSchemaSetV1:
    ordered = tuple(sorted(tool_schemas, key=lambda item: (item.name, item.version)))
    return CanonicalToolSchemaSetV1(
        tool_version=tool_version,
        tool_schemas=ordered,
        digest=_schema_set_digest(tool_version=tool_version, schemas=ordered),
    )


@dataclass(frozen=True, slots=True)
class CanonicalPrefixCachePolicyV1:
    """Exact retained prompt-prefix policy; route supplies only provider identity."""

    policy_version: str
    prefix_message_count: int
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("prefix-cache policy version must be non-empty")
        if (
            isinstance(self.prefix_message_count, bool)
            or not isinstance(self.prefix_message_count, int)
            or not 1 <= self.prefix_message_count <= 256
        ):
            raise ValueError("prefix-cache message count is outside hard bounds")
        expected = sha256_lowerhex(
            canonical_json(
                {
                    "policy_version": self.policy_version,
                    "prefix_message_count": self.prefix_message_count,
                }
            ).encode("utf-8")
        )
        if self.digest != expected:
            raise ValueError("prefix-cache policy digest differs from exact policy")


@dataclass(frozen=True, slots=True)
class CanonicalPrefixCachePolicyRefV1:
    policy_version: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("prefix-cache policy ref version must be non-empty")
        if (
            not isinstance(self.digest, str)
            or len(self.digest) != 64
            or any(character not in "0123456789abcdef" for character in self.digest)
        ):
            raise ValueError("prefix-cache policy ref digest must be lowercase SHA-256")


def build_prefix_cache_policy(
    *, policy_version: str, prefix_message_count: int
) -> CanonicalPrefixCachePolicyV1:
    digest = sha256_lowerhex(
        canonical_json(
            {
                "policy_version": policy_version,
                "prefix_message_count": prefix_message_count,
            }
        ).encode("utf-8")
    )
    return CanonicalPrefixCachePolicyV1(
        policy_version=policy_version,
        prefix_message_count=prefix_message_count,
        digest=digest,
    )


def _request_configuration_digest(binding: "CanonicalPromptBindingV1") -> str:
    prefix = binding.prefix_cache_policy_ref
    return sha256_lowerhex(
        canonical_json(
            {
                "request_params": json.loads(binding.request_params_canonical_json),
                "model_request_schema_versions": list(binding.model_request_schema_versions),
                "policy_injected_params": [
                    {
                        "name": item.name,
                        "minimum": item.minimum,
                        "maximum": item.maximum,
                        "required": item.required,
                    }
                    for item in binding.policy_injected_params
                ],
                "tool_schema_set_ref": {
                    "tool_version": binding.tool_schema_set_ref.tool_version,
                    "digest": binding.tool_schema_set_ref.digest,
                },
                "prefix_cache_policy_ref": (
                    None
                    if prefix is None
                    else {
                        "policy_version": prefix.policy_version,
                        "digest": prefix.digest,
                    }
                ),
            }
        ).encode("utf-8")
    )


@dataclass(frozen=True, slots=True, order=True)
class CanonicalPolicyInjectedParamV1:
    """One optional positive-integer field injected by exact routing policy."""

    name: str
    minimum: int
    maximum: int
    required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("policy-injected parameter name must be non-empty")
        if (
            isinstance(self.minimum, bool)
            or not isinstance(self.minimum, int)
            or isinstance(self.maximum, bool)
            or not isinstance(self.maximum, int)
            or self.minimum < 1
            or self.maximum < self.minimum
        ):
            raise ValueError("policy-injected parameter bound is invalid")


@dataclass(frozen=True, slots=True)
class CanonicalPromptBindingV1:
    """Exact retained authority for one agent-node/prompt-version pair."""

    binding_id: str
    agent_node_id: str
    prompt_version: str
    renderer_version: str
    source_slots: tuple[CanonicalPromptSourceSlotV1, ...]
    max_source_count: int
    max_source_bytes: int
    doc_version_source_slot_id: str
    rendered_artifact_source_kind_registry_version: int
    rendered_artifact_source_kind_id: str
    request_params_canonical_json: str
    model_request_schema_versions: tuple[str, ...]
    policy_injected_params: tuple[CanonicalPolicyInjectedParamV1, ...]
    tool_schema_set_ref: CanonicalToolSchemaSetRefV1
    prefix_cache_policy_ref: CanonicalPrefixCachePolicyRefV1 | None
    template_messages: tuple[RetainedTemplateMessageV1, ...]
    source_renderer: SourceRenderer

    def __post_init__(self) -> None:
        for name in (
            "binding_id",
            "agent_node_id",
            "prompt_version",
            "renderer_version",
            "doc_version_source_slot_id",
            "rendered_artifact_source_kind_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty")
        if (
            isinstance(self.rendered_artifact_source_kind_registry_version, bool)
            or not isinstance(self.rendered_artifact_source_kind_registry_version, int)
            or self.rendered_artifact_source_kind_registry_version < 1
        ):
            raise ValueError("rendered Artifact source-kind registry version must be positive")
        if (
            isinstance(self.max_source_count, bool)
            or not isinstance(self.max_source_count, int)
            or not 1 <= self.max_source_count <= MAX_PROMPT_SOURCE_COUNT
        ):
            raise ValueError("binding max_source_count is outside hard bounds")
        if (
            isinstance(self.max_source_bytes, bool)
            or not isinstance(self.max_source_bytes, int)
            or not 1 <= self.max_source_bytes <= MAX_PROMPT_SOURCE_BYTES
        ):
            raise ValueError("binding max_source_bytes is outside hard bounds")
        slot_ids = tuple(slot.slot_id for slot in self.source_slots)
        if not slot_ids or slot_ids != tuple(sorted(set(slot_ids))):
            raise ValueError("canonical prompt source slots must be stable-unique")
        if sum(slot.min_count for slot in self.source_slots) > self.max_source_count:
            raise ValueError("slot minimum cardinality exceeds binding aggregate count")
        shapes = tuple(shape for slot in self.source_slots for shape in slot.allowed_shapes)
        if len(shapes) != len(set(shapes)):
            raise ValueError("one exact source shape cannot belong to multiple prompt slots")
        primary = next(
            (slot for slot in self.source_slots if slot.slot_id == self.doc_version_source_slot_id),
            None,
        )
        if primary is None or primary.min_count != 1 or primary.max_count != 1:
            raise ValueError("doc-version source slot must have exact cardinality one")
        try:
            parsed_params = json.loads(self.request_params_canonical_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("request params authority must be canonical JSON") from exc
        if (
            not isinstance(parsed_params, dict)
            or canonical_json(parsed_params) != self.request_params_canonical_json
            or len(self.request_params_canonical_json.encode("utf-8"))
            > MAX_PROMPT_REQUEST_PARAMS_BYTES
        ):
            raise ValueError("request params authority must be one canonical JSON object")
        injected_names = tuple(item.name for item in self.policy_injected_params)
        if injected_names != tuple(sorted(set(injected_names))):
            raise ValueError("policy-injected params must be stable-unique")
        if set(parsed_params).intersection(injected_names):
            raise ValueError("base request params overlap policy-injected params")
        if (
            not self.model_request_schema_versions
            or self.model_request_schema_versions
            != tuple(sorted(set(self.model_request_schema_versions)))
            or set(self.model_request_schema_versions) - {"model-router@1", "model-router@2"}
        ):
            raise ValueError("model request schema versions are not exact retained values")
        if not self.template_messages:
            raise ValueError("canonical prompt binding requires retained template authority")
        if not callable(self.source_renderer):
            raise ValueError("canonical prompt binding requires a source renderer")


@dataclass(frozen=True, slots=True)
class CanonicalPromptRenderV1:
    """Exact messages, PromptParts, and carrying-Artifact provenance seed."""

    binding_id: str
    renderer_version: str
    source_artifact_ids: tuple[str, ...]
    inherited_doc_version: str
    agent_tool_version: str
    tool_schemas: tuple[ToolSchemaRef, ...]
    request_configuration_digest: str
    messages: tuple[Message, ...]
    prompt_parts: tuple[PromptPartV1, ...]
    rendered_artifact_source_kind_registry_version: int
    rendered_artifact_source_kind_id: str
    rendered_origin_ref: OriginRefV1
    rendered_trust: TrustLevel
    aggregate_input_hash: str

    def provenance_for_output(self, output_hash: str) -> ProvenanceV1:
        """Build the ``source_rendered`` carrying-Artifact provenance."""

        return ProvenanceV1(
            source_kind_registry_version=(self.rendered_artifact_source_kind_registry_version),
            source_kind_id=self.rendered_artifact_source_kind_id,
            origin_ref=self.rendered_origin_ref,
            parent_source_artifact_ids=self.source_artifact_ids,
            connector_id=self.binding_id,
            connector_version=self.renderer_version,
            trust=self.rendered_trust,
            source_hash=output_hash,
            transformations=(
                ProvenanceTransformationV1(
                    tool_version=self.renderer_version,
                    input_hash=self.aggregate_input_hash,
                    output_hash=output_hash,
                ),
            ),
        )


class CanonicalPromptRendererAuthority:
    """Versioned, fail-closed prompt-template and source-renderer authority."""

    def __init__(
        self,
        *,
        source_kind_registries: Iterable[SourceKindRegistryV1],
        tool_schema_sets: Iterable[CanonicalToolSchemaSetV1] = (),
        prefix_cache_policies: Iterable[CanonicalPrefixCachePolicyV1] = (),
        bindings: Iterable[CanonicalPromptBindingV1],
    ) -> None:
        registries: dict[int, SourceKindRegistryV1] = {}
        for registry in source_kind_registries:
            retained = registries.get(registry.registry_version)
            if retained is not None and retained != registry:
                raise ValueError("source-kind registry version has conflicting history")
            registries[registry.registry_version] = registry
        if not registries:
            raise ValueError("canonical prompt authority requires source-kind history")

        schema_sets: dict[tuple[str, str], _ResolvedToolSchemaSet] = {}
        tool_versions: dict[str, str] = {}
        for schema_set in tool_schema_sets:
            if schema_set.digest != _schema_set_digest(
                tool_version=schema_set.tool_version,
                schemas=schema_set.tool_schemas,
            ):
                raise ValueError("tool schema set changed after retained construction")
            retained_digest = tool_versions.get(schema_set.tool_version)
            if retained_digest is not None and retained_digest != schema_set.digest:
                raise ValueError("tool version has conflicting schema-set history")
            schema_sets[(schema_set.tool_version, schema_set.digest)] = _ResolvedToolSchemaSet(
                tool_version=schema_set.tool_version,
                identities=tuple(
                    (schema.name, schema.version) for schema in schema_set.tool_schemas
                ),
            )
            tool_versions[schema_set.tool_version] = schema_set.digest

        prefix_policies: dict[tuple[str, str], CanonicalPrefixCachePolicyV1] = {}
        prefix_versions: dict[str, str] = {}
        for policy in prefix_cache_policies:
            retained_digest = prefix_versions.get(policy.policy_version)
            if retained_digest is not None and retained_digest != policy.digest:
                raise ValueError("prefix-cache policy version has conflicting history")
            prefix_policies[(policy.policy_version, policy.digest)] = policy
            prefix_versions[policy.policy_version] = policy.digest

        by_key: dict[tuple[str, str], CanonicalPromptBindingV1] = {}
        binding_ids: set[str] = set()
        for binding in bindings:
            key = (binding.agent_node_id, binding.prompt_version)
            if key in by_key:
                raise ValueError("canonical prompt binding key must be unique")
            if binding.binding_id in binding_ids:
                raise ValueError("canonical prompt binding identity must be unique")
            schema_ref = binding.tool_schema_set_ref
            if (schema_ref.tool_version, schema_ref.digest) not in schema_sets:
                raise ValueError("prompt binding tool schema-set authority is unavailable")
            prefix_ref = binding.prefix_cache_policy_ref
            if (
                prefix_ref is not None
                and (
                    prefix_ref.policy_version,
                    prefix_ref.digest,
                )
                not in prefix_policies
            ):
                raise ValueError("prompt binding prefix-cache policy authority is unavailable")
            registry = registries.get(binding.rendered_artifact_source_kind_registry_version)
            if registry is None or registry.get(binding.rendered_artifact_source_kind_id) is None:
                raise ValueError("rendered Artifact source kind is absent from registry history")
            for slot in binding.source_slots:
                for shape in slot.allowed_shapes:
                    registry = registries.get(shape.source_kind_registry_version)
                    definition = None if registry is None else registry.get(shape.source_kind_id)
                    if definition is None:
                        raise ValueError("slot source kind is absent from registry history")
                    if not set(slot.allowed_prompt_purposes).issubset(
                        definition.allowed_prompt_purposes
                    ):
                        raise ValueError(
                            "slot prompt purpose exceeds its exact source-kind authority"
                        )
            by_key[key] = binding
            binding_ids.add(binding.binding_id)
        self._registries = registries
        self._schema_sets = schema_sets
        self._prefix_policies = prefix_policies
        self._bindings = by_key

    @property
    def binding_keys(self) -> tuple[tuple[str, str], ...]:
        """Stable retained keys for composition readiness checks."""

        return tuple(sorted(self._bindings))

    @property
    def binding_plan_keys(self) -> tuple[tuple[str, str, str], ...]:
        """Exact node/prompt/tool closure consumed by frozen Agent graphs."""

        return tuple(
            sorted(
                (
                    binding.agent_node_id,
                    binding.prompt_version,
                    binding.tool_schema_set_ref.tool_version,
                )
                for binding in self._bindings.values()
            )
        )

    @property
    def allowed_prefix_policy_refs(self) -> tuple[CanonicalPrefixCachePolicyRefV1, ...]:
        refs = {
            binding.prefix_cache_policy_ref
            for binding in self._bindings.values()
            if binding.prefix_cache_policy_ref is not None
        }
        return tuple(sorted(refs, key=lambda item: (item.policy_version, item.digest)))

    @property
    def allowed_prefix_policy_versions(self) -> frozenset[str]:
        """Exact-binding-derived router admission view; never a second allowlist."""

        return frozenset(ref.policy_version for ref in self.allowed_prefix_policy_refs)

    def require_source_metadata_bounds(
        self,
        *,
        agent_node_id: str,
        prompt_version: str,
        source_artifacts: tuple[ArtifactV2, ...],
    ) -> None:
        """Validate complete source shape/count/size before any blob is opened."""

        binding = self._bindings.get((agent_node_id, prompt_version))
        if binding is None:
            raise IntegrityViolation(
                "exact canonical prompt binding is unavailable",
                agent_node_id=agent_node_id,
                prompt_version=prompt_version,
            )
        self._verify_source_metadata(binding=binding, source_artifacts=source_artifacts)

    def render(
        self,
        *,
        agent_node_id: str,
        prompt_version: str,
        sources: tuple[tuple[ArtifactV2, bytes], ...],
    ) -> CanonicalPromptRenderV1:
        binding = self._bindings.get((agent_node_id, prompt_version))
        if binding is None:
            raise IntegrityViolation(
                "exact canonical prompt binding is unavailable",
                agent_node_id=agent_node_id,
                prompt_version=prompt_version,
            )
        verified = self._verify_sources(binding=binding, sources=sources)
        schema_ref = binding.tool_schema_set_ref
        schema_set = self._schema_sets[(schema_ref.tool_version, schema_ref.digest)]
        tool_schemas = tuple(
            ToolSchemaRef(name=name, version=version) for name, version in schema_set.identities
        )
        by_id = {source.artifact.artifact_id: source for source in verified}
        parent_ids = tuple(by_id)
        primary = next(
            source for source in verified if source.slot_id == binding.doc_version_source_slot_id
        )
        doc_version = primary.artifact.version_tuple.doc_version
        if not isinstance(doc_version, str) or not doc_version:
            raise IntegrityViolation("primary prompt source lacks inherited doc_version")

        outer_registry = self._registries[binding.rendered_artifact_source_kind_registry_version]
        outer_definition = outer_registry.get(binding.rendered_artifact_source_kind_id)
        if outer_definition is None:
            raise IntegrityViolation("rendered Artifact source kind authority disappeared")
        overall_trust = most_conservative_trust(
            tuple(source.provenance.trust for source in verified)
        )
        if overall_trust not in outer_definition.allowed_trust_levels:
            raise IntegrityViolation(
                "most-conservative source trust is forbidden for rendered Artifact source kind"
            )

        template_messages = tuple(
            self._validate_template_message(message, registries=self._registries)
            for message in binding.template_messages
        )
        source_messages = self._render_source_twice(binding=binding, sources=verified)
        full_input_hash = self._aggregate_input_hash(verified)
        outer_origin = OriginRefV1(
            opaque_source_id=f"prompt-binding:{binding.binding_id}",
            source_revision=full_input_hash,
        )

        messages: list[Message] = [message[0] for message in template_messages]
        parts: list[PromptPartV1] = [message[1] for message in template_messages]
        slots = {slot.slot_id: slot for slot in binding.source_slots}
        contributed_source_ids: set[str] = set()
        for rendered in source_messages:
            unknown = tuple(
                source_id for source_id in rendered.source_artifact_ids if source_id not in by_id
            )
            if unknown:
                raise IntegrityViolation(
                    "canonical source message cites a non-input source Artifact",
                    source_artifact_ids=unknown,
                )
            contributors = tuple(by_id[source_id] for source_id in rendered.source_artifact_ids)
            contributed_source_ids.update(rendered.source_artifact_ids)
            for contributor in contributors:
                slot = slots[contributor.slot_id]
                if (
                    rendered.purpose not in slot.allowed_prompt_purposes
                    or rendered.purpose not in contributor.source_kind.allowed_prompt_purposes
                ):
                    raise IntegrityViolation(
                        "canonical source message purpose is forbidden for its contributor",
                        source_artifact_id=contributor.artifact.artifact_id,
                        purpose=rendered.purpose,
                    )
            rendered_registry = self._registries.get(rendered.rendered_source_kind_registry_version)
            rendered_definition = (
                None
                if rendered_registry is None
                else rendered_registry.get(rendered.rendered_source_kind_id)
            )
            part_trust = most_conservative_trust(
                tuple(source.provenance.trust for source in contributors)
            )
            if (
                rendered_definition is None
                or rendered.purpose not in rendered_definition.allowed_prompt_purposes
                or part_trust not in rendered_definition.allowed_trust_levels
            ):
                raise IntegrityViolation(
                    "canonical source message kind/purpose/trust is not authoritative"
                )
            subset_hash = self._aggregate_input_hash(contributors)
            origin = OriginRefV1(
                opaque_source_id=(
                    f"prompt-part:{binding.binding_id}:{rendered.rendered_source_kind_id}"
                ),
                source_revision=subset_hash,
            )
            output_hash = sha256_lowerhex(rendered.text.encode("utf-8"))
            derived = ProvenanceV1(
                source_kind_registry_version=(rendered.rendered_source_kind_registry_version),
                source_kind_id=rendered.rendered_source_kind_id,
                origin_ref=origin,
                parent_source_artifact_ids=rendered.source_artifact_ids,
                connector_id=binding.binding_id,
                connector_version=binding.renderer_version,
                trust=part_trust,
                source_hash=output_hash,
                transformations=(
                    ProvenanceTransformationV1(
                        tool_version=binding.renderer_version,
                        input_hash=subset_hash,
                        output_hash=output_hash,
                    ),
                ),
            )
            try:
                part = PromptPartV1(
                    text=rendered.text,
                    provenance=derived,
                    purpose=rendered.purpose,
                )
            except ValueError as exc:
                raise IntegrityViolation(
                    "canonical source renderer produced an invalid PromptPartV1"
                ) from exc
            messages.append(Message(role=rendered.role, content=rendered.text))
            parts.append(part)
        if contributed_source_ids != set(parent_ids):
            raise IntegrityViolation(
                "canonical source messages do not cover the exact prompt source set"
            )
        if not messages or len(messages) != len(parts):
            raise IntegrityViolation("canonical prompt renderer produced no complete prompt")
        return CanonicalPromptRenderV1(
            binding_id=binding.binding_id,
            renderer_version=binding.renderer_version,
            source_artifact_ids=parent_ids,
            inherited_doc_version=doc_version,
            agent_tool_version=schema_set.tool_version,
            tool_schemas=tool_schemas,
            request_configuration_digest=_request_configuration_digest(binding),
            messages=tuple(messages),
            prompt_parts=tuple(parts),
            rendered_artifact_source_kind_registry_version=(
                binding.rendered_artifact_source_kind_registry_version
            ),
            rendered_artifact_source_kind_id=(binding.rendered_artifact_source_kind_id),
            rendered_origin_ref=outer_origin,
            rendered_trust=overall_trust,
            aggregate_input_hash=full_input_hash,
        )

    def require_model_request(
        self,
        *,
        model_request: ModelRequestV1 | ModelRequestV2,
        sources: tuple[tuple[ArtifactV2, bytes], ...],
    ) -> CanonicalPromptRenderV1:
        rendered = self.render(
            agent_node_id=model_request.agent_node_id,
            prompt_version=model_request.prompt_version,
            sources=sources,
        )
        if tuple(model_request.messages) != rendered.messages:
            raise IntegrityViolation(
                "handler rendered messages differ from canonical prompt authority"
            )
        self._require_request_configuration(
            binding=self._bindings[(model_request.agent_node_id, model_request.prompt_version)],
            rendered=rendered,
            model_request=model_request,
        )
        return rendered

    def _require_request_configuration(
        self,
        *,
        binding: CanonicalPromptBindingV1,
        rendered: CanonicalPromptRenderV1,
        model_request: ModelRequestV1 | ModelRequestV2,
    ) -> None:
        if model_request.model_router_schema_version not in (binding.model_request_schema_versions):
            raise IntegrityViolation(
                "model request schema version differs from canonical prompt authority"
            )
        actual_params = dict(model_request.params)
        for rule in binding.policy_injected_params:
            if rule.name not in actual_params:
                if rule.required:
                    raise IntegrityViolation(
                        "model request lacks required policy-injected parameter",
                        parameter=rule.name,
                    )
                continue
            value = actual_params.pop(rule.name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not rule.minimum <= value <= rule.maximum
            ):
                raise IntegrityViolation(
                    "model request policy-injected parameter is outside retained bounds",
                    parameter=rule.name,
                )
        try:
            actual_params_json = canonical_json(actual_params)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "handler request params are not canonical bounded JSON"
            ) from exc
        if actual_params_json != binding.request_params_canonical_json:
            raise IntegrityViolation(
                "handler request params differ from canonical prompt authority"
            )
        if tuple(model_request.tool_schemas) != rendered.tool_schemas:
            raise IntegrityViolation(
                "handler tool schema set differs from exact tool-version authority"
            )

        actual_directive: PrefixCacheDirectiveV1 | None
        if isinstance(model_request, ModelRequestV2):
            actual_directive = model_request.prefix_cache_directive
        else:
            if model_request.cache_key is not None:
                raise IntegrityViolation(
                    "legacy semantic cache hint is not canonical prompt authority"
                )
            actual_directive = None
        policy_ref = binding.prefix_cache_policy_ref
        if policy_ref is None:
            if actual_directive is not None:
                raise IntegrityViolation(
                    "handler prefix-cache directive lacks exact retained policy authority"
                )
            return
        if not isinstance(model_request, ModelRequestV2):
            raise IntegrityViolation("prefix-cache policy requires ModelRequestV2")
        policy = self._prefix_policies[(policy_ref.policy_version, policy_ref.digest)]
        if policy.prefix_message_count > len(rendered.messages):
            raise IntegrityViolation(
                "retained prefix-cache policy exceeds canonical rendered messages"
            )
        expected_directive = PrefixCacheDirectiveV1(
            prefix_message_count=policy.prefix_message_count,
            prefix_hash=compute_prefix_hash(rendered.messages[: policy.prefix_message_count]),
            provider_scope=model_request.model_snapshot.provider,
            policy_version=policy.policy_version,
        )
        if actual_directive != expected_directive:
            raise IntegrityViolation(
                "handler prefix-cache directive differs from exact policy authority"
            )

    def _verify_sources(
        self,
        *,
        binding: CanonicalPromptBindingV1,
        sources: tuple[tuple[ArtifactV2, bytes], ...],
    ) -> tuple[CanonicalPromptSourceV1, ...]:
        metadata = self._verify_source_metadata(
            binding=binding,
            source_artifacts=tuple(item[0] for item in sources),
        )
        verified: list[CanonicalPromptSourceV1] = []
        for retained, (_, source_payload) in zip(metadata, sources, strict=True):
            payload_hash = sha256_lowerhex(source_payload)
            source_artifact = retained.artifact
            if (
                payload_hash != source_artifact.payload_hash
                or source_artifact.object_ref.size_bytes != len(source_payload)
            ):
                raise IntegrityViolation(
                    "prompt source bytes differ from immutable Artifact",
                    source_artifact_id=source_artifact.artifact_id,
                )
            verified.append(
                CanonicalPromptSourceV1(
                    slot_id=retained.slot_id,
                    artifact=source_artifact,
                    payload=source_payload,
                    provenance=retained.provenance,
                    source_kind=retained.source_kind,
                )
            )
        return tuple(verified)

    def _verify_source_metadata(
        self,
        *,
        binding: CanonicalPromptBindingV1,
        source_artifacts: tuple[ArtifactV2, ...],
    ) -> tuple[_VerifiedPromptSourceMetadata, ...]:
        if not source_artifacts:
            raise IntegrityViolation("canonical prompt requires at least one source Artifact")
        ids = tuple(item.artifact_id for item in source_artifacts)
        if ids != tuple(sorted(set(ids))):
            raise IntegrityViolation("prompt source Artifact ids must be stable-unique")
        if len(source_artifacts) > binding.max_source_count:
            raise IntegrityViolation("prompt source count exceeds retained binding bound")
        total_bytes = sum(item.object_ref.size_bytes for item in source_artifacts)
        if total_bytes > binding.max_source_bytes:
            raise IntegrityViolation("prompt source bytes exceed retained aggregate bound")

        verified: list[_VerifiedPromptSourceMetadata] = []
        counts = {slot.slot_id: 0 for slot in binding.source_slots}
        byte_counts = {slot.slot_id: 0 for slot in binding.source_slots}
        for source_artifact in source_artifacts:
            payload_schema_id = source_artifact.meta.get("payload_schema_id")
            if source_artifact.object_ref.sha256 != source_artifact.payload_hash:
                raise IntegrityViolation(
                    "prompt source ObjectRef differs from immutable Artifact",
                    source_artifact_id=source_artifact.artifact_id,
                )
            raw_provenance = source_artifact.meta.get("provenance")
            if raw_provenance is None:
                raise IntegrityViolation(
                    "prompt source lacks retained ProvenanceV1",
                    source_artifact_id=source_artifact.artifact_id,
                )
            try:
                provenance = ProvenanceV1.model_validate(raw_provenance)
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation(
                    "prompt source provenance is invalid",
                    source_artifact_id=source_artifact.artifact_id,
                ) from exc
            if (
                provenance.source_hash != source_artifact.payload_hash
                or provenance.parent_source_artifact_ids != tuple(source_artifact.lineage)
            ):
                raise IntegrityViolation(
                    "prompt source provenance/hash/parent lineage differs",
                    source_artifact_id=source_artifact.artifact_id,
                )
            registry = self._registries.get(provenance.source_kind_registry_version)
            definition = None if registry is None else registry.get(provenance.source_kind_id)
            if definition is None or provenance.trust not in definition.allowed_trust_levels:
                raise IntegrityViolation(
                    "prompt source kind/trust is absent from exact registry",
                    source_artifact_id=source_artifact.artifact_id,
                )
            if not isinstance(payload_schema_id, str) or not payload_schema_id:
                raise IntegrityViolation(
                    "prompt source lacks an exact payload schema identity",
                    source_artifact_id=source_artifact.artifact_id,
                )
            shape = CanonicalPromptSourceShapeV1(
                artifact_kind=source_artifact.kind,
                payload_schema_id=payload_schema_id,
                source_kind_registry_version=provenance.source_kind_registry_version,
                source_kind_id=provenance.source_kind_id,
            )
            matching = tuple(slot for slot in binding.source_slots if shape in slot.allowed_shapes)
            if len(matching) != 1:
                raise IntegrityViolation(
                    "prompt source does not resolve one exact typed slot",
                    source_artifact_id=source_artifact.artifact_id,
                )
            slot = matching[0]
            counts[slot.slot_id] += 1
            byte_counts[slot.slot_id] += source_artifact.object_ref.size_bytes
            verified.append(
                _VerifiedPromptSourceMetadata(
                    slot_id=slot.slot_id,
                    artifact=source_artifact,
                    provenance=provenance,
                    source_kind=definition,
                )
            )
        for slot in binding.source_slots:
            count = counts[slot.slot_id]
            if count < slot.min_count or count > slot.max_count:
                raise IntegrityViolation(
                    "prompt source slot cardinality differs from retained binding",
                    slot_id=slot.slot_id,
                    actual_count=count,
                    min_count=slot.min_count,
                    max_count=slot.max_count,
                )
            if byte_counts[slot.slot_id] > slot.max_bytes:
                raise IntegrityViolation(
                    "prompt source slot bytes exceed retained binding bound",
                    slot_id=slot.slot_id,
                    actual_bytes=byte_counts[slot.slot_id],
                    max_bytes=slot.max_bytes,
                )
        return tuple(verified)

    @staticmethod
    def _aggregate_input_hash(sources: tuple[CanonicalPromptSourceV1, ...]) -> str:
        return sha256_lowerhex(
            canonical_json(
                [
                    {
                        "artifact_id": source.artifact.artifact_id,
                        "payload_hash": source.artifact.payload_hash,
                    }
                    for source in sources
                ]
            ).encode("utf-8")
        )

    @staticmethod
    def _render_source_twice(
        *,
        binding: CanonicalPromptBindingV1,
        sources: tuple[CanonicalPromptSourceV1, ...],
    ) -> tuple[CanonicalSourceMessageV1, ...]:
        try:
            first = tuple(binding.source_renderer(sources))
            second = tuple(binding.source_renderer(sources))
        except Exception as exc:
            raise IntegrityViolation("canonical source renderer failed") from exc
        if first != second:
            raise IntegrityViolation("canonical source renderer is nondeterministic")
        if not first or any(not isinstance(item, CanonicalSourceMessageV1) for item in first):
            raise IntegrityViolation("canonical source renderer returned an invalid message set")
        return first

    @staticmethod
    def _validate_template_message(
        retained: RetainedTemplateMessageV1,
        *,
        registries: dict[int, SourceKindRegistryV1],
    ) -> tuple[Message, PromptPartV1]:
        provenance = retained.part.provenance
        registry = registries.get(provenance.source_kind_registry_version)
        definition = None if registry is None else registry.get(provenance.source_kind_id)
        if (
            definition is None
            or provenance.trust not in definition.allowed_trust_levels
            or retained.part.purpose not in definition.allowed_prompt_purposes
            or retained.part.purpose != "instruction"
            or provenance.parent_source_artifact_ids
        ):
            raise IntegrityViolation("retained prompt template provenance is not authoritative")
        expected_hash = sha256_lowerhex(retained.part.text.encode("utf-8"))
        if provenance.source_hash != expected_hash:
            raise IntegrityViolation("retained prompt template hash differs from exact text")
        return Message(role=retained.role, content=retained.part.text), retained.part


__all__ = [
    "CanonicalPolicyInjectedParamV1",
    "CanonicalPrefixCachePolicyRefV1",
    "CanonicalPrefixCachePolicyV1",
    "CanonicalPromptBindingV1",
    "CanonicalPromptRenderV1",
    "CanonicalPromptRendererAuthority",
    "CanonicalPromptSourceShapeV1",
    "CanonicalPromptSourceSlotV1",
    "CanonicalPromptSourceV1",
    "CanonicalSourceMessageV1",
    "CanonicalToolSchemaSetRefV1",
    "CanonicalToolSchemaSetV1",
    "MAX_PROMPT_SOURCE_BYTES",
    "MAX_PROMPT_SOURCE_COUNT",
    "RetainedTemplateMessageV1",
    "build_prefix_cache_policy",
    "build_tool_schema_set",
]

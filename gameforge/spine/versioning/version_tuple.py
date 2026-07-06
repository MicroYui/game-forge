"""VersionTuple builder + content-addressed artifact id (contract §5).

`build_version_tuple` fills the fields known at this milestone
(ir_snapshot_id / seed / tool_version / env_contract_version) and leaves the
rest (constraint_snapshot_id / prompt_version / model_snapshot /
agent_graph_version / cassette_id / doc_version) as schema-present defaults —
producers for those arrive in later milestones (不简化只延后).

`artifact_id_for` is content-addressed: the same (kind, version_tuple,
payload_hash) always yields the same artifact_id, independent of insertion
order or process.
"""

from __future__ import annotations

from gameforge.contracts.canonical import compute_snapshot_id
from gameforge.contracts.lineage import ArtifactKind, VersionTuple
from gameforge.contracts.versions import ENV_CONTRACT_VERSION, TOOL_VERSION


def build_version_tuple(
    *,
    ir_snapshot_id: str | None = None,
    seed: int | None = None,
    tool_version: str = TOOL_VERSION,
    env_contract_version: str = ENV_CONTRACT_VERSION,
    **overrides,
) -> VersionTuple:
    return VersionTuple(
        ir_snapshot_id=ir_snapshot_id,
        seed=seed,
        tool_version=tool_version,
        env_contract_version=env_contract_version,
        **overrides,
    )


def artifact_id_for(kind: ArtifactKind, version_tuple: VersionTuple, payload_hash: str | None) -> str:
    return compute_snapshot_id(
        {
            "kind": kind,
            "version_tuple": version_tuple.model_dump(exclude_none=True),
            "payload_hash": payload_hash,
        }
    )

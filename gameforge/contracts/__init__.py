"""GameForge contracts — schema single source of truth."""

from gameforge.contracts.agent_io import (  # noqa: F401
    AgentNodeResult, AgentRole,
)
from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord  # noqa: F401
from gameforge.contracts.model_router import (  # noqa: F401
    Message, ModelRequest, ModelResponse, ModelSnapshot, ToolSchemaRef, request_hash,
)
from gameforge.contracts.versions import (  # noqa: F401
    AGENT_IO_SCHEMA_VERSION,
    AUDIT_SCHEMA_VERSION,
    CASSETTE_SCHEMA_VERSION,
    DSL_GRAMMAR_VERSION,
    ENV_CONTRACT_VERSION,
    FINDING_SCHEMA_VERSION,
    IR_SCHEMA_VERSION,
    LINEAGE_SCHEMA_VERSION,
    META_SCHEMA_VERSION,
    MODEL_ROUTER_SCHEMA_VERSION,
    PATCH_SCHEMA_VERSION,
    REVIEW_SCHEMA_VERSION,
    TOOL_VERSION,
)

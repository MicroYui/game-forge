"""Version-bound SLO evaluation and replayable alert policy."""

from gameforge.platform.slo.alerts import AlertStateMachine, AlertTransitionResult
from gameforge.platform.slo.evaluator import SLOEvaluator, SLOEvaluatorLimits
from gameforge.platform.slo.repository import (
    AlertStateRepository,
    InMemoryAlertStateRepository,
)
from gameforge.platform.slo.service import (
    DEFAULT_SLO_RECONCILIATION_LIMIT,
    MetricDescriptorRetainer,
    SLODefinitionCapabilities,
    SLODefinitionCapabilityBinder,
    SLODefinitionRepository,
    SLODefinitionService,
    SLODefinitionUnitOfWork,
    SLO_RETENTION_RECONCILIATION_OWNER_ID,
)

__all__ = [
    "AlertStateMachine",
    "AlertStateRepository",
    "AlertTransitionResult",
    "DEFAULT_SLO_RECONCILIATION_LIMIT",
    "InMemoryAlertStateRepository",
    "MetricDescriptorRetainer",
    "SLODefinitionCapabilities",
    "SLODefinitionCapabilityBinder",
    "SLODefinitionRepository",
    "SLODefinitionService",
    "SLODefinitionUnitOfWork",
    "SLO_RETENTION_RECONCILIATION_OWNER_ID",
    "SLOEvaluator",
    "SLOEvaluatorLimits",
]

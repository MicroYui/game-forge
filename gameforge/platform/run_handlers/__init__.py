"""Trusted platform Run handlers that are available before worker composition."""

from gameforge.platform.run_handlers.bench import BenchRunHandler
from gameforge.platform.run_handlers.checker import CheckerRunHandler, DefaultCheckerFactory
from gameforge.platform.run_handlers.constraint_proposal import ConstraintProposalHandler
from gameforge.platform.run_handlers.deferred import (
    DEFERRED_EXECUTORS,
    artifact_migration_deferred,
    dr_drill_deferred,
)
from gameforge.platform.run_handlers.generation import GenerationProposalHandler
from gameforge.platform.run_handlers.model_routing import (
    BridgeModelRouter,
    ModelBridgeAgentAdapter,
)
from gameforge.platform.run_handlers.repair import RepairSearchHandler
from gameforge.platform.run_handlers.review import ReviewRunHandler
from gameforge.platform.run_handlers.simulation import SimulationRunHandler
from gameforge.platform.run_handlers.task_suite import (
    ScenarioDerivationRequest,
    ScenarioDraftV1,
    ScenarioShaper,
    TaskSuiteDeriveHandler,
)

__all__ = [
    "DEFERRED_EXECUTORS",
    "BenchRunHandler",
    "BridgeModelRouter",
    "CheckerRunHandler",
    "ConstraintProposalHandler",
    "DefaultCheckerFactory",
    "GenerationProposalHandler",
    "ModelBridgeAgentAdapter",
    "RepairSearchHandler",
    "ReviewRunHandler",
    "ScenarioDerivationRequest",
    "ScenarioDraftV1",
    "ScenarioShaper",
    "SimulationRunHandler",
    "TaskSuiteDeriveHandler",
    "artifact_migration_deferred",
    "dr_drill_deferred",
]

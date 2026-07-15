"""Trusted platform Run handlers that are available before worker composition."""

from gameforge.platform.run_handlers.bench import BenchRunHandler
from gameforge.platform.run_handlers.checker import CheckerRunHandler, DefaultCheckerFactory
from gameforge.platform.run_handlers.deferred import (
    DEFERRED_EXECUTORS,
    DeferredExecutionRequest,
    artifact_migration_deferred,
    dr_drill_deferred,
)
from gameforge.platform.run_handlers.model_routing import ModelBridgeAgentAdapter
from gameforge.platform.run_handlers.review import ReviewRunHandler
from gameforge.platform.run_handlers.simulation import SimulationRunHandler

__all__ = [
    "DEFERRED_EXECUTORS",
    "BenchRunHandler",
    "CheckerRunHandler",
    "DefaultCheckerFactory",
    "DeferredExecutionRequest",
    "ModelBridgeAgentAdapter",
    "ReviewRunHandler",
    "SimulationRunHandler",
    "artifact_migration_deferred",
    "dr_drill_deferred",
]

"""Pure role-based authorization over frozen M4 identity contracts."""

from gameforge.platform.rbac.authorization import AuthorizationDecision, authorize

__all__ = ["AuthorizationDecision", "authorize"]

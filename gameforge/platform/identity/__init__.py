"""Local identity authentication, session, bootstrap, and management services."""

from gameforge.platform.identity.authentication import (
    ApiKeyAuthenticationService,
    TrustedSystemActorFactory,
)
from gameforge.platform.identity.bootstrap import BootstrapService
from gameforge.platform.identity.management import IdentityManagementService
from gameforge.platform.identity.sessions import (
    SessionAuthenticationService,
    TransactionalSessionManager,
)

__all__ = [
    "ApiKeyAuthenticationService",
    "BootstrapService",
    "IdentityManagementService",
    "SessionAuthenticationService",
    "TransactionalSessionManager",
    "TrustedSystemActorFactory",
]

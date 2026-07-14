"""Package-level anchor for the split Task 3 identity service suites."""

from gameforge.platform.identity import (
    ApiKeyAuthenticationService,
    BootstrapService,
    IdentityManagementService,
    SessionAuthenticationService,
    TransactionalSessionManager,
    TrustedSystemActorFactory,
)


def test_identity_package_exposes_the_complete_task3_service_boundary() -> None:
    assert {
        service.__name__
        for service in (
            ApiKeyAuthenticationService,
            BootstrapService,
            IdentityManagementService,
            SessionAuthenticationService,
            TransactionalSessionManager,
            TrustedSystemActorFactory,
        )
    } == {
        "ApiKeyAuthenticationService",
        "BootstrapService",
        "IdentityManagementService",
        "SessionAuthenticationService",
        "TransactionalSessionManager",
        "TrustedSystemActorFactory",
    }

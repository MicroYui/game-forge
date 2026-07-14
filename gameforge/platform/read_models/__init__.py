"""Authorized read-model composition services."""

from gameforge.platform.read_models.authorization import (
    AuthorizedReadCollection,
    ReadAuthorizationBinding,
    ReadAuthorizationService,
    ReadPolicyRepository,
    authorization_fingerprint,
    principal_binding,
    principal_identity_binding,
)

__all__ = [
    "AuthorizedReadCollection",
    "ReadAuthorizationBinding",
    "ReadAuthorizationService",
    "ReadPolicyRepository",
    "authorization_fingerprint",
    "principal_binding",
    "principal_identity_binding",
]

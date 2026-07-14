from __future__ import annotations

from pydantic import ValidationError
import pytest

from gameforge.contracts.api import (
    ApprovalDecisionRequestV1,
    ApprovalRequirementProgressV1,
    ApprovalViewV1,
    ApiKeyAuthRequestV1,
    ApiKeyAuthenticator,
    ApiKeyRecordV1,
    ApiKeySecret,
    AuthenticationResultV1,
    IdentityAuthenticator,
    LoginNameNormalizationPolicyV1,
    OidcCallbackV1,
    OidcCode,
    OidcProvider,
    PasswordAuthRequestV1,
    PasswordCredentialRecordV1,
    PasswordHashPolicyV1,
    RunAcceptedV1,
    RunViewV1,
    SecretText,
    SessionIssueV1,
    SessionManager,
    SessionPolicyV1,
    SessionRecordV1,
    SessionToken,
    compute_login_name_normalization_policy_digest,
    encode_sse_event,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import Problem, RunEvent, RunEventEnvelope, RunQueuedDataV1
from gameforge.contracts.execution_profiles import RunKindRef


def _normalization_policy() -> LoginNameNormalizationPolicyV1:
    payload = {
        "policy_version": "login-normalization/1",
        "unicode_normalization": "NFKC",
        "trim_unicode_whitespace": True,
        "case_mapping": "unicode_casefold",
        "reject_categories": ("private_use", "control", "surrogate"),
        "minimum_codepoints": 3,
        "maximum_codepoints": 128,
    }
    return LoginNameNormalizationPolicyV1(
        **payload,
        policy_digest=compute_login_name_normalization_policy_digest(payload),
    )


@pytest.mark.parametrize(
    ("payload", "secret"),
    [
        (
            PasswordAuthRequestV1(login_name="designer", password=SecretText("pw-secret")),
            "pw-secret",
        ),
        (ApiKeyAuthRequestV1(api_key=ApiKeySecret("key-secret")), "key-secret"),
        (
            OidcCallbackV1(
                provider_id="local-oidc",
                state=SecretText("state-secret"),
                code=OidcCode("code-secret"),
                redirect_uri_id="console",
            ),
            "code-secret",
        ),
    ],
)
def test_transport_secrets_are_redacted_from_repr_and_serialization(
    payload: object, secret: str
) -> None:
    rendered = repr(payload)
    dumped = payload.model_dump_json()  # type: ignore[attr-defined]

    assert secret not in rendered
    assert secret not in dumped
    assert "**********" in dumped
    for value in payload.__dict__.values():
        if isinstance(value, SecretText):
            assert secret not in str(value)


@pytest.mark.parametrize(
    ("factory", "maximum"),
    [
        (
            lambda value: PasswordAuthRequestV1(
                login_name="designer",
                password=SecretText(value),
            ),
            4096,
        ),
        (
            lambda value: ApiKeyAuthRequestV1(api_key=ApiKeySecret(value)),
            4096,
        ),
        (
            lambda value: OidcCallbackV1(
                provider_id="local-oidc",
                state=SecretText(value),
                code=OidcCode("code"),
                redirect_uri_id="console",
            ),
            4096,
        ),
        (
            lambda value: OidcCallbackV1(
                provider_id="local-oidc",
                state=SecretText("state"),
                code=OidcCode(value),
                redirect_uri_id="console",
            ),
            8192,
        ),
    ],
)
def test_transport_secret_fields_are_nonempty_and_hard_bounded(factory, maximum: int) -> None:
    assert factory("x" * maximum)
    with pytest.raises(ValidationError):
        factory("")
    with pytest.raises(ValidationError):
        factory("x" * (maximum + 1))


def test_auth_policy_is_canonical_and_digest_bound() -> None:
    policy = _normalization_policy()

    assert policy.reject_categories == ("control", "private_use", "surrogate")
    with pytest.raises(ValidationError, match="policy_digest"):
        LoginNameNormalizationPolicyV1(
            **{
                **policy.model_dump(mode="python"),
                "maximum_codepoints": 129,
            }
        )


def test_authentication_protocols_are_runtime_structural() -> None:
    class CompleteAdapter:
        def verify_password(self, request):
            raise NotImplementedError

        def authenticate(self, request):
            raise NotImplementedError

        def issue(self, request):
            raise NotImplementedError

        def resolve(self, token, *, csrf_token, request_method):
            raise NotImplementedError

        def revoke(self, session_id, *, expected_revision, reason, actor):
            raise NotImplementedError

        def begin(self, request):
            raise NotImplementedError

        def complete(self, callback):
            raise NotImplementedError

    adapter = CompleteAdapter()

    assert isinstance(adapter, IdentityAuthenticator)
    assert isinstance(adapter, ApiKeyAuthenticator)
    assert isinstance(adapter, SessionManager)
    assert isinstance(adapter, OidcProvider)


def test_auth_policy_rejects_inverted_bounds() -> None:
    policy = _normalization_policy()

    with pytest.raises(ValidationError, match="maximum_codepoints"):
        LoginNameNormalizationPolicyV1(
            **{
                **policy.model_dump(mode="python"),
                "minimum_codepoints": 129,
                "maximum_codepoints": 128,
                "policy_digest": compute_login_name_normalization_policy_digest(
                    {
                        **policy.model_dump(mode="python", exclude={"policy_digest"}),
                        "minimum_codepoints": 129,
                    }
                ),
            }
        )

    with pytest.raises(ValidationError, match="maximum_codepoints"):
        LoginNameNormalizationPolicyV1(
            **{
                **policy.model_dump(mode="python"),
                "maximum_codepoints": 257,
                "policy_digest": compute_login_name_normalization_policy_digest(
                    {
                        **policy.model_dump(mode="python", exclude={"policy_digest"}),
                        "maximum_codepoints": 257,
                    }
                ),
            }
        )


def test_auth_records_freeze_credential_and_session_state() -> None:
    normalization = _normalization_policy()
    password = PasswordCredentialRecordV1(
        credential_id="password-1",
        principal_id="human-1",
        normalized_login_name="designer",
        normalization_policy_version=normalization.policy_version,
        normalization_policy_digest=normalization.policy_digest,
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$redacted$redacted",
        hash_policy_version="argon2/1",
        credential_version=1,
        status="active",
        changed_at="2026-07-14T00:00:00Z",
        revision=1,
    )
    api_key = ApiKeyRecordV1(
        api_key_id="api-key-1",
        principal_id="service-1",
        key_prefix="gf_live_12ab",
        key_digest="a" * 64,
        credential_version=1,
        status="active",
        created_at="2026-07-14T00:00:00Z",
        revision=1,
    )
    session = SessionRecordV1(
        session_id="session-1",
        principal_id="human-1",
        source_credential_id=password.credential_id,
        credential_version=password.credential_version,
        token_digest="b" * 64,
        csrf_secret_digest="c" * 64,
        signing_key_id="session-key-1",
        issued_at="2026-07-14T00:00:00Z",
        absolute_expires_at="2026-07-15T00:00:00Z",
        idle_expires_at="2026-07-14T01:00:00Z",
        last_seen_at="2026-07-14T00:00:00Z",
        revision=1,
    )

    assert password.status == "active"
    assert api_key.status == "active"
    assert session.revoked_at is None
    with pytest.raises(ValidationError, match="revocation"):
        SessionRecordV1(
            **{
                **session.model_dump(mode="python"),
                "revoke_reason": "logout",
            }
        )


def test_policy_and_authentication_result_invariants() -> None:
    hash_policy = PasswordHashPolicyV1(
        policy_version="argon2/1",
        algorithm="argon2id",
        memory_kib=65_536,
        iterations=3,
        parallelism=4,
        salt_bytes=16,
        rehash_on_login=True,
        effective_from="2026-07-14T00:00:00Z",
    )
    session_policy = SessionPolicyV1(
        policy_version="session/1",
        absolute_ttl_s=86_400,
        idle_ttl_s=3_600,
        touch_interval_s=60,
        signing_key_set_version="session-keys/1",
        csrf_mode="synchronizer_token",
        same_site="strict",
        secure_cookie_required=True,
    )
    authn = AuthenticationResultV1(
        principal_id="human-1",
        principal_kind="human",
        credential_id="password-1",
        credential_version=1,
        authenticated_at="2026-07-14T00:00:00Z",
    )

    assert hash_policy.algorithm == "argon2id"
    assert session_policy.idle_ttl_s < session_policy.absolute_ttl_s
    assert authn.principal_kind == "human"
    with pytest.raises(ValidationError, match="idle_ttl_s"):
        SessionPolicyV1(
            **{
                **session_policy.model_dump(mode="python"),
                "idle_ttl_s": 90_000,
            }
        )


def test_one_time_session_secrets_remain_redacted() -> None:
    issued = SessionIssueV1(
        session_id="session-1",
        session_token=SessionToken("signed-session-token"),
        csrf_token=SecretText("csrf-secret"),
        absolute_expires_at="2026-07-15T00:00:00Z",
        idle_expires_at="2026-07-14T01:00:00Z",
    )

    assert "signed-session-token" not in issued.model_dump_json()
    assert "csrf-secret" not in issued.model_dump_json()


def test_api_module_reuses_authoritative_problem_and_event_contracts() -> None:
    from gameforge.contracts.api import Problem as ApiProblem
    from gameforge.contracts.api import RunEventEnvelope as ApiRunEventEnvelope

    assert ApiProblem is Problem
    assert ApiRunEventEnvelope is RunEventEnvelope


def test_run_transport_views_do_not_expose_worker_fencing() -> None:
    accepted = RunAcceptedV1(
        run_id="run-1",
        status_url="/api/v1/runs/run-1",
        events_url="/api/v1/runs/run-1/events",
    )
    queued = RunViewV1(
        run_id="run-1",
        status="queued",
        revision=1,
        status_url=accepted.status_url,
        events_url=accepted.events_url,
    )
    succeeded = RunViewV1(
        run_id="run-1",
        status="succeeded",
        revision=7,
        attempt_no=2,
        result_artifact_id="run-result-1",
        terminal_cassette_artifact_id="cassette-run-1",
        status_url=accepted.status_url,
        events_url=accepted.events_url,
    )

    assert queued.attempt_no is None
    assert succeeded.result_artifact_id == "run-result-1"
    assert "fencing" not in RunViewV1.model_json_schema()["properties"]
    assert "lease_id" not in RunViewV1.model_json_schema()["properties"]
    with pytest.raises(ValidationError, match="failure_artifact_id"):
        RunViewV1(
            **{
                **succeeded.model_dump(mode="python"),
                "status": "failed",
            }
        )


@pytest.mark.parametrize(
    ("status", "attempt_no"),
    [
        ("queued", None),
        ("leased", 1),
        ("running", 1),
        ("retry_wait", 1),
    ],
)
def test_nonterminal_run_view_rejects_terminal_cassette(
    status: str,
    attempt_no: int | None,
) -> None:
    with pytest.raises(ValidationError, match="terminal_cassette_artifact_id"):
        RunViewV1(
            run_id="run-1",
            status=status,
            revision=1,
            attempt_no=attempt_no,
            terminal_cassette_artifact_id="artifact:cassette",
            status_url="/api/v1/runs/run-1",
            events_url="/api/v1/runs/run-1/events",
        )


def test_approval_transport_projection_is_canonical_and_count_bound() -> None:
    request = ApprovalDecisionRequestV1(
        decision="approve",
        requirement_ids=("numeric", "content", "numeric"),
        expected_workflow_revision=4,
        reason_code="reviewed",
    )
    progress = ApprovalRequirementProgressV1(
        requirement_id="numeric",
        domain_scope=DomainScope(domain_ids=("economy",)),
        route_role="numeric_designer",
        min_approvals=2,
        valid_approval_count=1,
        satisfied=False,
        eligible_for_current_actor=True,
        unmet_distinct_from_requirement_ids=("content",),
    )

    assert request.requirement_ids == ("content", "numeric")
    assert progress.satisfied is False
    with pytest.raises(ValidationError, match="satisfied"):
        ApprovalRequirementProgressV1(
            **{
                **progress.model_dump(mode="python"),
                "satisfied": True,
            }
        )

    schema = ApprovalViewV1.model_json_schema()["properties"]
    assert schema["requirement_progress"]["maxItems"] == 1024
    assert schema["current_actor_allowed_requirement_ids"]["maxItems"] == 1024


def test_sse_wire_is_canonical_and_contains_no_worker_lease_data() -> None:
    event = RunEvent(
        run_id="run-1",
        seq=1,
        event_type="run.queued",
        occurred_at="2026-07-14T00:00:00Z",
        data_schema_version="run-queued@1",
        data=RunQueuedDataV1(
            run_kind=RunKindRef(kind="checker.run", version=1),
            queue_deadline_utc="2026-07-14T00:05:00Z",
            overall_deadline_utc="2026-07-14T01:00:00Z",
        ),
        trace_id="1" * 32,
    )

    wire = encode_sse_event(event)

    assert wire.startswith("id:1\nevent:run.queued\ndata:{")
    assert wire.endswith("\n\n")
    assert '"run_id":"run-1"' in wire
    assert "lease_id" not in wire
    assert "fencing_token" not in wire

"""Trusted identity CLI adapter; provisioning stays in the platform service."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import getpass
import hmac
import json
import sys
from typing import Protocol, TextIO

from pydantic import ValidationError

from gameforge.apps.cli.identity import (
    IdentityBootstrapConfigurationError,
    build_bootstrap_service_from_environment,
)
from gameforge.contracts.auth import SecretText
from gameforge.contracts.errors import GameForgeError
from gameforge.platform.identity.bootstrap import (
    BootstrapAdminRequest,
    BootstrapResult,
)


PasswordReader = Callable[[str], str]


class BootstrapServicePort(Protocol):
    def bootstrap(self, request: BootstrapAdminRequest) -> BootstrapResult: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gameforge identity")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--display-name", required=True)
    bootstrap.add_argument("--login-name", required=True)
    return parser


def _write_json(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, sort_keys=True) + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    service: BootstrapServicePort | None = None,
    password_reader: PasswordReader = getpass.getpass,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Parse one trusted identity command and call only the platform service."""

    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    arguments = _parser().parse_args(argv)
    if arguments.command != "bootstrap":
        raise RuntimeError("identity CLI parsed an unsupported command")

    if service is None:
        try:
            service = build_bootstrap_service_from_environment()
        except IdentityBootstrapConfigurationError:
            _write_json(
                err,
                {
                    "code": "identity_bootstrap_configuration_invalid",
                    "status": "rejected",
                },
            )
            return 2

    password = password_reader("Password: ")
    confirmation = password_reader("Confirm password: ")
    if not isinstance(password, str) or not isinstance(confirmation, str):
        _write_json(err, {"code": "invalid_password_input", "status": "rejected"})
        return 2
    if not hmac.compare_digest(password, confirmation):
        _write_json(
            err,
            {"code": "password_confirmation_mismatch", "status": "rejected"},
        )
        return 2

    try:
        request = BootstrapAdminRequest(
            display_name=arguments.display_name,
            login_name=arguments.login_name,
            password=SecretText(password),
        )
        result = service.bootstrap(request)
    except ValidationError:
        _write_json(err, {"code": "invalid_bootstrap_input", "status": "rejected"})
        return 2
    except GameForgeError as exc:
        _write_json(err, {"code": exc.code, "status": "rejected"})
        return 1

    _write_json(
        out,
        {
            "status": "created",
            "principal_id": result.principal_id,
            "principal_revision": result.principal_revision,
            "password_credential_id": result.password_credential_id,
            "roles": list(result.roles),
        },
    )
    return 0


__all__ = ["BootstrapServicePort", "main"]

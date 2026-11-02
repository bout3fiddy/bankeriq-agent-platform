"""Identity and input-admission dependencies supplied by the application."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    """Tenant and user resolved by the application's authentication provider."""

    tenant_id: str
    user_id: str


class AuthenticationRequired(Exception):
    """The transport caller could not be authenticated."""


class InputRejected(Exception):
    """Input policy did not admit this message for the external coordinator."""

"""Verify Entra access tokens before constructing tenant/user runtime context."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt
from jwt import PyJWKClient


@dataclass(frozen=True)
class EntraUser:
    tenant_id: str
    user_id: str


class EntraAuthentication:
    """Accept only delegated pilot users for this tenant, audience and client.

    For example, Alice's token for the runtime application resolves to her signed
    tid/oid pair. An ARM token, an app-only worker token or Bob's otherwise valid
    token is rejected. HTTP headers and request JSON cannot replace that identity.
    keys is an injected signing-key lookup for tests; production uses Microsoft's
    tenant-specific JWKS endpoint with bounded cache lifetime and network timeout.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        audience: str,
        users: frozenset[str],
        clients: frozenset[str],
        keys: Callable[[str], Any] | None = None,
    ) -> None:
        self.tenant = str(UUID(tenant_id))
        self.audience = str(UUID(audience))
        self.users = frozenset(str(UUID(user)) for user in users)
        self.clients = frozenset(str(UUID(client)) for client in clients)

        if not self.users or not self.clients:
            raise ValueError("explicit pilot users and client applications are required")

        self.issuer = f"https://login.microsoftonline.com/{self.tenant}/v2.0"
        client = PyJWKClient(
            f"https://login.microsoftonline.com/{self.tenant}/discovery/v2.0/keys",
            lifespan=300,
            timeout=5,
        )
        self.keys = keys or (lambda token: client.get_signing_key_from_jwt(token).key)

    def authenticate(self, authorization: str) -> EntraUser:

        # Validation happens before constructing RunContext. Bound the header
        # before parsing and accept only RS256; token-supplied algorithms/URLs
        # cannot select the verifier or the issuer's public-key source.
        if not authorization.startswith("Bearer ") or len(authorization) > 16384:
            raise ValueError("authentication failed")

        token = authorization[7:]

        try:
            claims = jwt.decode(
                token,
                self.keys(token),
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                leeway=30,
                options={"require": ["exp", "iat", "nbf", "tid", "oid", "azp", "scp", "ver"]},
            )

            if not isinstance(claims["oid"], str):
                raise ValueError("authentication failed")

            user = str(UUID(claims["oid"]))

            if (
                claims["ver"] != "2.0"
                or claims["tid"] != self.tenant
                or user not in self.users
                or claims["azp"] not in self.clients
                or not isinstance(claims["scp"], str)
                or "runtime.invoke" not in claims["scp"].split()
                or claims.get("idtyp") == "app"
            ):
                raise ValueError("authentication failed")
        except jwt.PyJWTError, ValueError, TypeError, KeyError:
            raise ValueError("authentication failed") from None

        return EntraUser(self.tenant, user)


def synthetic_input(context: object, text: str) -> str:
    """Permit only the reviewed fixture until a real input-admission policy exists.

    Token validation proves identity; it does not establish whether user text is
    safe for an external model. The hosted pilot therefore rejects every other
    message before durable acceptance or model execution.
    """

    if text not in (
        "Calculate the sum of 2 and 3.",
        "Calculate the sum of 2 and 3, then prepare a restricted report.",
    ):
        raise ValueError("only the synthetic pilot input is permitted")

    return text


def operator_test_input(context: object, text: str) -> str:
    """Accept bounded text in the explicitly enabled, operator-only test environment.

    The deploying user authorises non-sensitive conversations. This is an opt-in
    test policy, with Entra identity/user allowlisting enforced before this call;
    it provides no automated sensitivity classification for production input.
    """

    if not text.strip() or len(text.encode()) > 8192 or "\x00" in text:
        raise ValueError("invalid operator test message")

    return text

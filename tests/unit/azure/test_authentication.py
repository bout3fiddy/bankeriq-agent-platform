"""Real JWT signatures with synthetic keys; no Entra network calls."""

import time
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_platform.identity.entra import EntraAuthentication, synthetic_input


@pytest.fixture
def identity():
    tenant, user, audience, client = [str(uuid4()) for _ in range(4)]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = EntraAuthentication(
        tenant_id=tenant,
        audience=audience,
        users=frozenset([user]),
        clients=frozenset([client]),
        keys=lambda token: key.public_key(),
    )
    now = int(time.time())
    claims = {
        "tid": tenant,
        "oid": user,
        "aud": audience,
        "azp": client,
        "ver": "2.0",
        "iss": f"https://login.microsoftonline.com/{tenant}/v2.0",
        "scp": "runtime.invoke",
        "exp": now + 300,
        "iat": now,
        "nbf": now,
    }
    return auth, key, claims


def test_verified_identity_is_scoped(identity):
    auth, key, claims = identity
    user = auth.authenticate("Bearer " + jwt.encode(claims, key, algorithm="RS256"))
    assert (user.tenant_id, user.user_id) == (claims["tid"], claims["oid"])


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": str(uuid4())},
        {"tid": str(uuid4())},
        {"oid": str(uuid4())},
        {"oid": 17},
        {"azp": str(uuid4())},
        {"scp": "other"},
        {"scp": None},
        {"idtyp": "app"},
        {"exp": 1},
        {"nbf": 9999999999},
        {"ver": "1.0"},
        {"iss": "https://invalid"},
    ],
)
def test_invalid_tokens_never_supply_context(identity, changes):
    auth, key, claims = identity
    token = jwt.encode({**claims, **changes}, key, algorithm="RS256")

    with pytest.raises(ValueError, match="authentication failed"):
        auth.authenticate("Bearer " + token)


def test_signature_and_header_are_required(identity):
    auth, _, claims = identity
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    for header in (
        "",
        "Bearer " + "x" * 20000,
        "Bearer " + jwt.encode(claims, wrong_key, algorithm="RS256"),
    ):
        with pytest.raises(ValueError, match="authentication failed"):
            auth.authenticate(header)


@pytest.mark.parametrize(
    "text",
    [
        "Calculate the sum of 2 and 3.",
        "Calculate the sum of 2 and 3, then prepare a restricted report.",
    ],
)
def test_pilot_does_not_admit_arbitrary_user_text(text):
    assert synthetic_input(None, text) == text

    with pytest.raises(ValueError):
        synthetic_input(None, "arbitrary user data")

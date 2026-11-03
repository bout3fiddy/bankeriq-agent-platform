"""Explicit credential selection and renewable tokens without actual Azure access."""

from collections.abc import Callable

import pytest

from agent_runtime.providers.foundry.credentials import foundry_credential


@pytest.mark.parametrize("mode", ["azure_cli", "managed_identity"])
def test_credentials_are_lazy_renewable_and_closed(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    events: list[object] = []

    class Credential:
        def __init__(self, **kwargs: object) -> None:
            events.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            events.append("closed")

    def token_provider(credential: Credential, scope: str) -> Callable[[], str]:
        events.append(scope)

        def token() -> str:
            events.append("token requested")
            return "synthetic-token"

        return token

    monkeypatch.setattr("azure.identity.AzureCliCredential", Credential)
    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", Credential)
    monkeypatch.setattr("azure.identity.get_bearer_token_provider", token_provider)

    with foundry_credential(
        {"FOUNDRY_AUTH_MODE": mode, "AZURE_TENANT_ID": "tenant", "AZURE_CLIENT_ID": "client"}
    ) as credential:
        assert callable(credential)
        assert "token requested" not in events
        assert credential() == "synthetic-token"
        assert credential() == "synthetic-token"

    assert events == [
        (
            {"tenant_id": "tenant", "process_timeout": 15}
            if mode == "azure_cli"
            else {"client_id": "client"}
        ),
        "https://cognitiveservices.azure.com/.default",
        "token requested",
        "token requested",
        "closed",
    ]


def test_unknown_auth_mode_does_not_fall_back_to_an_api_key() -> None:
    with (
        pytest.raises(ValueError, match="unsupported Foundry authentication mode"),
        foundry_credential({"FOUNDRY_AUTH_MODE": "unknown", "AZURE_OPENAI_API_KEY": "synthetic"}),
    ):
        pytest.fail("an unsupported authentication mode must fail")

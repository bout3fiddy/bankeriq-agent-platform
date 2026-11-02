"""Resolve one explicit Foundry authentication mode at the provider boundary."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager


@contextmanager
def foundry_credential(environment: Mapping[str, str]) -> Iterator[str | Callable[[], str]]:
    mode = environment.get("FOUNDRY_AUTH_MODE", "api_key")

    if mode == "api_key":
        yield environment["AZURE_OPENAI_API_KEY"]
    elif mode == "azure_cli":
        from azure.identity import AzureCliCredential, get_bearer_token_provider

        # Example: after az login, FOUNDRY_AUTH_MODE=azure_cli supplies renewable
        # tokens to the SDK. Tokens remain in memory and never become config outputs.
        with AzureCliCredential(
            tenant_id=environment.get("AZURE_TENANT_ID", ""), process_timeout=15
        ) as credential:
            yield get_bearer_token_provider(
                credential, "https://cognitiveservices.azure.com/.default"
            )
    else:
        raise ValueError("unsupported Foundry authentication mode")

"""Construct the SDK connection separately from model and conversation logic."""

from collections.abc import Callable

from openai import OpenAI


def create_foundry_client(
    *, base_url: str, api_key: str | Callable[[], str], timeout_seconds: float = 60
) -> OpenAI:
    """Create a client; the application owns credentials and closes the connection."""

    # Example base_url: https://my-resource.openai.azure.com/openai/v1/.
    # A renewable Entra token provider can be passed as api_key=token_provider.
    # Passing the callable lets the SDK obtain a token for each request.
    # Creating this client makes no inference call.
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout_seconds,
        max_retries=0,
    )

"""Select application-owned prompt assets before invoking the generic file adapter."""

from importlib.resources import files
from pathlib import Path

from agent_harness.prompts.catalog import PromptCatalog
from agent_runtime.prompts.files import load_catalog as read_catalog


def load_catalog(directory: Path | None = None) -> PromptCatalog:
    """Load an explicit directory or the application's packaged immutable catalog.

    Source checkouts use the repository prompts folder. An installed wheel uses
    agent_platform/prompt_data with the same byte-for-byte pinned JSON assets.
    For example, load_catalog(Path("reviewed-prompts")) selects that trusted
    directory; a model request cannot alter the startup path or retained versions.
    """

    if directory is None:
        source = Path(__file__).resolve().parents[3] / "prompts"
        directory = (
            source
            if (source / "catalog.json").is_file()
            else Path(str(files("agent_platform").joinpath("prompt_data")))
        )

    return read_catalog(directory)

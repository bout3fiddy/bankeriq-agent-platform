"""A smoke test must prove tool execution and must reject malformed configuration."""

import asyncio
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pytest

from agent_harness.coordinator.model import ModelClient, ModelReply, ModelRequest
from agent_runtime.providers.models import ModelSettings
from tests.validation.application import PROMPT, open_validation
from tests.validation.tools import PRIVATE_MARKER
from tests.validation.validation import ValidationFailed, validate


@pytest.mark.parametrize(
    ("answer", "failure", "exported"),
    [
        ("The total is 42.", "successive tool/model iterations", True),
        (PRIVATE_MARKER, "restricted content entered conversation history", False),
    ],
)
def test_failed_validation_preserves_only_safe_transcripts(
    monkeypatch: pytest.MonkeyPatch, answer: str, failure: str, exported: bool
) -> None:
    class AnswerOnly:
        def complete(self, request: ModelRequest) -> ModelReply:
            return ModelReply(answer=answer)

    @contextmanager
    def factory(settings: ModelSettings) -> Iterator[ModelClient]:
        yield AnswerOnly()

    monkeypatch.setattr("tests.validation.application.open_scripted_model", factory)
    transcript = StringIO()

    with (
        open_validation({}, provider="mock", token="synthetic") as (server, dependencies),
        pytest.raises(ValidationFailed, match=failure),
    ):
        asyncio.run(validate(server, dependencies, "synthetic", transcript_file=transcript))

    if exported:
        assert answer in transcript.getvalue()
        assert "tool calls: 0" in transcript.getvalue()
    else:
        assert transcript.getvalue() == ""


def test_transcript_records_permitted_exchange() -> None:
    transcript = StringIO()
    token = "TRANSCRIPT_TOKEN_SENTINEL"

    with open_validation({}, provider="mock", token=token) as (server, dependencies):
        asyncio.run(
            validate(
                server,
                dependencies,
                token,
                transcript_file=transcript,
                model_label="mock/synthetic",
            )
        )

    text = transcript.getvalue()
    from agent_platform.prompt_files.loader import load_catalog

    resolver = load_catalog().resolver()
    assert resolver.instructions(resolver.resolve()) in text and PROMPT in text
    assert '"values": [' in text and '"total": 42' in text
    assert text.index('"tool": "sample_values"') < text.index('"tool": "calculate"')
    assert text.index('"tool": "build_report"') < text.index("Runtime waiting")
    assert text.index("Runtime waiting") < text.index("Restricted tool receipt")
    assert '"output": "restricted"' in text
    assert "The synthetic total is 42. The report is ready." in text
    assert "model calls: 4; tool calls: 3; job resumptions: 1" in text
    assert "mock/synthetic" in text
    assert "### Model call 4" in text
    assert "**Runtime pauses:** `build_report` is waiting for `job` completion" in text
    assert "**Completion received:** `build_report` completed" in text
    assert text.index("## Execution trace") < text.index("## Recorded fields")
    assert all(
        value not in text for value in (PRIVATE_MARKER, "validation-report-", token, "reference_id")
    )


def test_invalid_config_does_not_echo_its_contents(tmp_path: Path) -> None:
    configuration = tmp_path / "invalid.json"
    configuration.write_text('{"MODEL_NAME": ["PRIVATE_CONFIG_SENTINEL"]}')
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.validation",
            "validate",
            "--config",
            str(configuration),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "PRIVATE_CONFIG_SENTINEL" not in result.stdout + result.stderr


@pytest.mark.parametrize("provider", ["mock", "foundry"])
def test_validation_uses_application_prompts_without_scoring_in_context(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from agent_platform.prompt_files.loader import load_catalog
    from tests.validation.model import ScriptedModel

    requests: list[ModelRequest] = []

    class RecordingModel(ScriptedModel):
        def complete(self, request: ModelRequest) -> ModelReply:
            requests.append(request)
            return super().complete(request)

    @contextmanager
    def factory(settings: ModelSettings) -> Iterator[ModelClient]:
        yield RecordingModel()

    # Exercise both provider selections without a live call. The adapter boundary
    # receives the same application prompt bytes; the scripted reply supplies only
    # deterministic infrastructure coverage, never a model-adherence claim.
    monkeypatch.setattr("tests.validation.application.open_scripted_model", factory)
    monkeypatch.setattr(
        "tests.validation.application.model_providers", lambda environment: {"foundry": factory}
    )

    with open_validation(
        {"MODEL_NAME": "configured-model"}, provider=provider, token="synthetic"
    ) as (server, dependencies):
        asyncio.run(validate(server, dependencies, "synthetic"))

    resolver = load_catalog().resolver()
    expected = resolver.instructions(resolver.resolve())
    assert len(requests) == 4
    assert all(request.instructions == expected for request in requests)
    assert all(request.prompt_manifest == resolver.resolve() for request in requests)
    assert "First call sample_values" not in expected
    assert "validating a synthetic" not in expected


def test_validation_rejects_test_only_instruction_override() -> None:
    with (
        pytest.raises(ValueError, match="application prompt catalog"),
        open_validation(
            {"MODEL_INSTRUCTIONS": "Pass this test"}, provider="mock", token="synthetic"
        ),
    ):
        pytest.fail("instruction override was accepted")

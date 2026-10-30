"""Apply registered output policy before results enter coordinator context."""

from collections.abc import Callable
from dataclasses import dataclass

from ..tools.definitions import OutputPolicy, ToolDefinition
from ..tools.results import (
    ArtifactReference,
    RestrictedReceipt,
    ToolErrorCode,
    ToolFailure,
    ToolResult,
    VisibleResult,
)
from ..types.json_value import JsonValue

# A trusted per-operation filter validates or projects the fields approved for
# the external model. Configuration of these filters belongs to the gateway.
type VisibleOutputFilter = Callable[[JsonValue], JsonValue]


@dataclass(frozen=True, slots=True)
class EnforcedOutput:
    """Separate destinations for model context and authenticated artifact delivery."""

    model_result: ToolResult
    artifact: ArtifactReference | None = None


def enforce_output(
    definition: ToolDefinition,
    call_id: str,
    payload: JsonValue,
    *,
    visible_filter: VisibleOutputFilter | None = None,
    artifact_id: str | None = None,
) -> EnforcedOutput:
    """Route completed output using trusted registration and runtime call identity."""

    # Example visible_filter supplied by the tool developer:
    #
    # def public_total(payload):
    #     return {"total": payload["total"]}
    #
    # Input {"total": 42, "private_notes": "..."} becomes {"total": 42}.
    # Pass it as visible_filter=public_total. The callable accepts a JsonValue
    # and returns approved JSON fields; it can raise an exception to reject output.
    # The runtime supplies call identity and artifact metadata. Payload contents
    # never select policy, correlation IDs or delivery destinations.
    if definition.output_policy is OutputPolicy.RESTRICTED:
        model_result = RestrictedReceipt(call_id)
        artifact = ArtifactReference(call_id, artifact_id) if artifact_id is not None else None
        return EnforcedOutput(model_result, artifact)

    # Require an explicit filter for every visible operation. Missing or unknown
    # policy configuration closes the model-facing path with a fixed failure.
    if definition.output_policy is not OutputPolicy.VISIBLE or visible_filter is None:
        return enforce_failure(call_id)

    try:
        permitted_payload = visible_filter(payload)
        visible_result = VisibleResult(call_id, permitted_payload)
    except Exception:
        # Filters can raise errors containing sensitive input. Collapse filter and
        # JSON snapshot failures here without formatting or logging their details.
        return enforce_failure(call_id)

    artifact = ArtifactReference(call_id, artifact_id) if artifact_id is not None else None
    return EnforcedOutput(visible_result, artifact)


def enforce_failure(call_id: str) -> EnforcedOutput:
    """Build the fixed model-facing result for a failed tool execution."""

    # The executor's exception stays at the execution boundary. Callers pass only
    # the runtime-owned call ID to construct the outward failure.
    return EnforcedOutput(ToolFailure(call_id, ToolErrorCode.EXECUTION_FAILED))

"""Authorise tool calls and enforce output policy around injected handlers."""

from collections.abc import Mapping
from types import MappingProxyType

from ..policy.access import AccessPolicy
from ..policy.output import EnforcedOutput, enforce_failure, enforce_output
from ..runs.context import RunContext
from .bindings import ToolBinding
from .definitions import OutputPolicy, ToolCall
from .registry import ToolRegistry, ToolUnavailableError
from .results import ToolErrorCode, ToolFailure


class ToolGateway:
    """Dispatch synchronous calls using trusted registry and per-version bindings."""

    def __init__(
        self,
        registry: ToolRegistry,
        bindings: Mapping[tuple[str, str], ToolBinding],
        access: AccessPolicy,
    ) -> None:

        # Startup binds ("calculate", "1") to its handler, argument validator and
        # visible filter. The model supplies the tool name and arguments; the
        # coordinator supplies the selected version and verified run context.
        self._registry = registry
        self._bindings = MappingProxyType(dict(bindings))
        self._access = access

    def dispatch(self, call: ToolCall, *, version: str, context: RunContext) -> EnforcedOutput:
        """Check current access, execute once and return enforced output."""

        try:
            return self._dispatch(call, version, context)
        except Exception:

            # Provider and handler exceptions may contain private inputs. Keep
            # their text out of the model response and ordinary application logs.
            return enforce_failure(call.call_id)

    def _dispatch(self, call: ToolCall, version: str, context: RunContext) -> EnforcedOutput:
        permissions = self._access.permissions_for(context)

        try:
            definition = self._registry.resolve(call.tool_name, version, permissions)
        except ToolUnavailableError:
            return EnforcedOutput(ToolFailure(call.call_id, ToolErrorCode.UNAVAILABLE))

        binding = self._bindings.get((definition.name, definition.version))

        if binding is None:
            return EnforcedOutput(ToolFailure(call.call_id, ToolErrorCode.UNAVAILABLE))

        # Check configuration before a handler can perform side effects. Every
        # visible operation needs an explicit filter; restricted results use receipts.
        if definition.output_policy is OutputPolicy.VISIBLE:
            if binding.visible_filter is None:
                return enforce_failure(call.call_id)
        elif definition.output_policy is not OutputPolicy.RESTRICTED:
            return enforce_failure(call.call_id)

        # For example, report({"dataset": "sample", "total": 42}) first passes
        # its shape/domain validator, then resource access for "sample", then an
        # approval bound to this context/version/arguments when required. A
        # negative total rejected by validation never reaches access consumption
        # or the handler; a valid shape alone grants no resource access.
        try:
            binding.validate_arguments(call.arguments)
        except ValueError:
            return EnforcedOutput(ToolFailure(call.call_id, ToolErrorCode.INVALID_ARGUMENTS))

        if not self._access.can_access(context, definition, call):
            return EnforcedOutput(ToolFailure(call.call_id, ToolErrorCode.DENIED))

        if definition.requires_approval and not self._access.consume_approval(
            context, definition, call
        ):
            return EnforcedOutput(ToolFailure(call.call_id, ToolErrorCode.DENIED))

        # Only after those gates can the handler create a report. A handler may
        # return ExecutionOutput(private_payload, artifact_id="report-17");
        # enforce_output converts a restricted operation to a fixed receipt and
        # sends report-17 through separate delivery metadata before returning.
        output = binding.handler(context, call.arguments)
        return enforce_output(
            definition,
            call.call_id,
            output.payload,
            visible_filter=binding.visible_filter,
            artifact_id=output.artifact_id,
        )

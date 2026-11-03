"""Immutable snapshots of validated JSON values."""

import math
from collections.abc import Mapping
from types import MappingProxyType

from agent_harness.types.json_value import JsonValue


def _freeze_object(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ValueError("expected a JSON object")

    # Example: ToolCall(..., arguments={"values": numbers}) captures [2, 3]
    # as an immutable tuple. Appending 4 to the caller's numbers list afterwards
    # cannot change the already-admitted call or its argument fingerprint.
    # Build a fresh dictionary and recursively snapshot every nested value.
    return MappingProxyType(
        {key: _freeze_entry(key, item, frozenset({id(value)})) for key, item in value.items()}
    )


def _freeze_entry(key: str, value: object, ancestors: frozenset[int]) -> JsonValue:
    if not isinstance(key, str):
        raise ValueError("JSON object keys must be strings")

    return _freeze_json(value, ancestors)


def _freeze_json(value: object, ancestors: frozenset[int]) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float) and math.isfinite(value):
        return value

    # Track the current ancestry path: values = []; values.append(values)
    # must fail before recursion loops forever. In contrast, [shared, shared]
    # is valid when shared has no cycle; each branch receives its own snapshot.
    # Each recursive branch extends its own immutable ancestry set to permit that reuse.
    if id(value) in ancestors:
        raise ValueError("JSON values must not contain cycles")

    nested = ancestors | {id(value)}

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_entry(key, item, nested) for key, item in value.items()}
        )

    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, nested) for item in value)

    raise ValueError("expected a finite JSON value")

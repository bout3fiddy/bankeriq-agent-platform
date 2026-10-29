"""Immutable snapshots of validated JSON values."""

import math
from collections.abc import Mapping
from types import MappingProxyType

from .json_value import JsonValue


def _freeze_object(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ValueError("expected a JSON object")

    # Wrap a fresh dictionary so later mutations of the caller's mapping leave
    # this snapshot unchanged.
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

    # Tracking the current ancestry path detects cycles while allowing shared
    # objects in separate branches to receive independent snapshots.
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

"""Recursive JSON values accepted by the runtime."""

from collections.abc import Mapping

# JSON objects and arrays are snapshotted into immutable containers so a caller
# cannot change arguments or policy schemas after validation or approval.
# This recursive type alias describes nested JSON without coupling the core to
# an SDK. Lists are accepted as input; construction converts them to tuples.
type JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | list[JsonValue]
    | tuple[JsonValue, ...]
    | Mapping[str, JsonValue]
)

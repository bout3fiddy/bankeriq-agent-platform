"""Method locking for the in-memory store."""

from collections.abc import Callable
from functools import wraps
from threading import RLock
from typing import Concatenate, Protocol


class LockOwner(Protocol):
    """The per-instance lock used by decorated storage methods."""

    _lock: RLock


def synchronized[Owner: LockOwner, **Parameters, Result](
    method: Callable[Concatenate[Owner, Parameters], Result],
) -> Callable[Concatenate[Owner, Parameters], Result]:
    """Hold the instance's lock for the complete method call, releasing it on exit."""

    # For example, @synchronized on save_checkpoint holds the lock through both
    # record updates. RLock also allows it to call decorated load_run on the same
    # thread. Each store instance owns its lock independently.
    @wraps(method)
    def locked(self: Owner, *args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked

"""Renew Azure database credentials when opening a physical primary connection."""

from collections.abc import Callable
from typing import Any

from psycopg import Connection


def token_connection(token: Callable[[], str]) -> type[Connection[dict[str, Any]]]:
    """Bind a renewable token source to connections without persisting a password.

    Example: token=lambda: credential.get_token(POSTGRES_SCOPE).token. A pool
    reuses authenticated sessions; every replacement session asks the credential
    for a currently valid token. Credentials and network clients stay in startup.
    The returned class does not modify global psycopg behaviour or another pool.
    """

    class RenewableConnection(Connection[dict[str, Any]]):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs: Any):

            # The SDK caches tokens until refresh is needed. A failed refresh
            # fails this connection; it never falls back to a stored password.
            return super().connect(conninfo, **{**kwargs, "password": token()})

    return RenewableConnection

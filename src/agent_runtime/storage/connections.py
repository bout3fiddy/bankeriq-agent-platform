"""Bounded primary connections owned by the application adapter lifecycle."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class PrimaryConnections:
    """Reuse at most size primary connections; size=0 preserves one-shot connections.

    For example, six processes with size=4 reserve at most 24 connections. Startup
    must include API, prompt and maintenance pools in the database-wide budget.
    A full pool queues at most twice its size and waits at most one second before
    raising an operational error. Call close() after draining application work.

    Startup can inject connection_type=token_connection(current_token) for Azure.
    Both pooled replacements and one-shot connections then request credentials
    when connecting; no cached password enters the DSN. Share this source with the
    store and prompt resolver so their combined usage remains inside one pool bound.
    """

    def __init__(
        self,
        dsn: str,
        *,
        size: int = 0,
        connection_type: type[Connection[dict[str, Any]]] = Connection[dict[str, Any]],
    ) -> None:
        if size < 0:
            raise ValueError("connection pool size cannot be negative")

        self.dsn = dsn
        self.connection_type = connection_type
        self.pool: ConnectionPool[Connection[dict[str, Any]]] | None = None

        if size:
            self.pool = ConnectionPool(
                dsn,
                connection_class=connection_type,
                kwargs={
                    "row_factory": dict_row,
                    "target_session_attrs": "read-write",
                    "connect_timeout": 3,
                },
                min_size=0,
                max_size=size,
                max_waiting=size * 2,
                timeout=1,
                max_idle=30,
                open=True,
            )

    @contextmanager
    def connection(self) -> Iterator[Connection[dict[str, Any]]]:

        # Both contexts commit on normal exit and roll back on exceptions. A
        # broken pooled connection is discarded; retries still resolve durable
        # request/operation identities before applying another database mutation.
        source = (
            self.pool.connection()
            if self.pool is not None
            else self.connection_type.connect(
                self.dsn, row_factory=dict_row, target_session_attrs="read-write"
            )
        )

        with source as connection:
            connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
            yield connection

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()

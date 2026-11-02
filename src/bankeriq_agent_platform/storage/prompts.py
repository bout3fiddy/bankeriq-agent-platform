"""Primary-backed global prompt publication, activation and immutable-body caching."""

from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import RLock
from typing import Any, LiteralString

import psycopg
from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from agent_runtime.hosting.access import Principal
from agent_runtime.prompts.resolution import (
    COMPOSER_VERSION,
    RUNTIME_INSTRUCTIONS,
    RUNTIME_VERSION,
    PromptManifest,
    PromptScope,
    PromptUnavailableError,
    compose,
    content_hash,
    validate_body,
)
from agent_runtime.storage.interface import StateConflictError

from .codec import fingerprint
from .postgres import CommitUnknownError, Database


class _PromptDatabase:
    """Keep writer routing and recovery fencing common to admin and read-only APIs."""

    def __init__(self, dsn: str, scope: PromptScope) -> None:
        if not scope.application or not scope.environment:
            raise ValueError("prompt application and environment are required")

        self._dsn = dsn
        self.scope = scope

        with self._connect() as connection:
            row = connection.execute("SELECT recovery_id FROM runtime_generation").fetchone()
            assert row is not None
            self._recovery_id = row["recovery_id"]

    def _connect(self) -> Database:
        connection = Connection[dict[str, Any]].connect(
            self._dsn, row_factory=dict_row, target_session_attrs="read-write"
        )
        connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        return connection

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[Database]:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT recovery_id FROM runtime_generation FOR SHARE"
                    if write
                    else "SELECT recovery_id FROM runtime_generation"
                ).fetchone()

                if row is None or row["recovery_id"] != self._recovery_id:
                    raise PromptUnavailableError("prompt database recovery generation changed")

                yield connection
        except psycopg.Error:
            if write:
                raise

            raise PromptUnavailableError("prompt primary is unavailable") from None

    def _key(self) -> tuple[str, str]:
        return self.scope.application, self.scope.environment


class PostgresPromptResolver(_PromptDatabase):
    """Read current bindings on every resolution; cache at most 64 immutable version bodies."""

    def __init__(self, dsn: str, scope: PromptScope) -> None:
        super().__init__(dsn, scope)
        self._bodies: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._lock = RLock()

    def resolve(self) -> PromptManifest:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT v.version, v.content_hash, v.runtime_version, v.runtime_hash, "
                "v.composer_version, "
                "b.revision FROM prompt_bindings b JOIN prompt_versions v "
                "ON (v.application, v.environment, v.version) = "
                "(b.application, b.environment, b.active_version) "
                "WHERE b.application = %s AND b.environment = %s AND NOT EXISTS "
                "(SELECT 1 FROM prompt_withdrawals w WHERE "
                "(w.application, w.environment, w.version) = "
                "(v.application, v.environment, v.version))",
                self._key(),
            ).fetchone()

        if row is None:
            raise PromptUnavailableError("no available global prompt binding")

        manifest = PromptManifest(
            self.scope,
            row["version"],
            row["content_hash"],
            row["revision"],
            row["runtime_version"],
            row["runtime_hash"],
            row["composer_version"],
        )
        self.instructions(manifest)
        return manifest

    def instructions(self, manifest: PromptManifest) -> str:
        if manifest.scope != self.scope:
            raise PromptUnavailableError("prompt manifest belongs to another deployment")

        # Read availability and verify the exact activation on the primary even
        # for a warm body. A withdrawal must fence an old prepared step too.
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT v.content_hash, v.runtime_version, v.runtime_hash, v.composer_version "
                "FROM prompt_versions v JOIN prompt_operations a USING "
                "(application, environment, version) WHERE v.application = %s "
                "AND v.environment = %s AND v.version = %s AND a.action = 'activate' "
                "AND a.revision = %s AND NOT EXISTS (SELECT 1 FROM prompt_withdrawals w "
                "WHERE (w.application, w.environment, w.version) = "
                "(v.application, v.environment, v.version))",
                (*self._key(), manifest.version, manifest.activation_revision),
            ).fetchone()

            if (
                row is None
                or row["content_hash"] != manifest.content_hash
                or row["runtime_hash"] != manifest.runtime_hash
                or row["runtime_version"] != manifest.runtime_version
                or row["composer_version"] != manifest.composer_version
            ):
                raise PromptUnavailableError("recorded prompt is unavailable or incompatible")

            key = (manifest.version, manifest.content_hash)

            with self._lock:
                body = self._bodies.get(key)

                if body is None:
                    record = connection.execute(
                        "SELECT body FROM prompt_versions WHERE application = %s "
                        "AND environment = %s AND version = %s",
                        (*self._key(), manifest.version),
                    ).fetchone()
                    assert record is not None
                    body = record["body"]
                    self._bodies[key] = body

                self._bodies.move_to_end(key)

                if len(self._bodies) > 64:
                    self._bodies.popitem(last=False)

        return compose(manifest, body)


class PromptAdministration(_PromptDatabase):
    """Management API supplied with a trusted principal and explicit application authorizer.

    For example, authorize=lambda actor, scope: actor.user_id in operators[scope]
    uses a server-owned operator list. No chat command or model tool exposes this API.
    Use a separate database identity from workers; infrastructure grants are external.
    """

    def __init__(
        self,
        dsn: str,
        scope: PromptScope,
        *,
        authorize: Callable[[Principal, PromptScope], bool],
        fault: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(dsn, scope)
        self._authorize = authorize
        self._fault = fault or (lambda phase: None)

    def active_binding(self, actor: Principal) -> tuple[str | None, int]:
        """Read management CAS inputs even when the active version has been withdrawn."""

        self._check(actor)

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT active_version, revision FROM prompt_bindings "
                "WHERE application = %s AND environment = %s",
                self._key(),
            ).fetchone()

        return (row["active_version"], row["revision"]) if row else (None, 0)

    def publish(
        self,
        actor: Principal,
        *,
        request_id: str,
        version: str,
        body: str,
        validation_reference: str,
        runtime_version: str = RUNTIME_VERSION,
        composer_version: str = COMPOSER_VERSION,
    ) -> str:
        """Publish a reviewed immutable body; activation is an explicit CAS operation."""

        self._check(actor)
        validate_body(body)

        if not version or not validation_reference or len(validation_reference) > 1024:
            raise ValueError("version and bounded validation evidence are required")

        if runtime_version != RUNTIME_VERSION or composer_version != COMPOSER_VERSION:
            raise ValueError("unsupported runtime or composer version")

        digest = content_hash(body)
        payload = [
            digest,
            validation_reference,
            runtime_version,
            content_hash(RUNTIME_INSTRUCTIONS),
            composer_version,
        ]

        def write(connection: Database, revision: int) -> int:
            self._write(
                connection,
                "INSERT INTO prompt_versions (application, environment, version, body, "
                "content_hash, runtime_version, runtime_hash, composer_version, "
                "author_tenant, author_user, "
                "validation_reference) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    *self._key(),
                    version,
                    body,
                    digest,
                    runtime_version,
                    content_hash(RUNTIME_INSTRUCTIONS),
                    composer_version,
                    actor.tenant_id,
                    actor.user_id,
                    validation_reference,
                ),
            )
            return revision

        self._operation(actor, request_id, "publish", version, payload, write)
        return digest

    def activate(
        self, actor: Principal, *, request_id: str, version: str, expected_revision: int
    ) -> int:
        """Publish or roll back the active binding, preserving every activation revision."""

        self._check(actor)

        def write(connection: Database, revision: int) -> int:
            if revision != expected_revision:
                raise StateConflictError("prompt binding revision changed")

            row = connection.execute(
                "SELECT runtime_version, runtime_hash, composer_version FROM prompt_versions v "
                "WHERE application = %s AND environment = %s AND version = %s "
                "AND NOT EXISTS (SELECT 1 FROM prompt_withdrawals w WHERE "
                "(w.application, w.environment, w.version) = "
                "(v.application, v.environment, v.version))",
                (*self._key(), version),
            ).fetchone()

            if (
                row is None
                or row["runtime_hash"] != content_hash(RUNTIME_INSTRUCTIONS)
                or row["runtime_version"] != RUNTIME_VERSION
                or row["composer_version"] != COMPOSER_VERSION
            ):
                raise PromptUnavailableError("activation requires a compatible available version")

            self._write(
                connection,
                "UPDATE prompt_bindings SET active_version = %s, revision = revision + 1 "
                "WHERE application = %s AND environment = %s AND revision = %s",
                (version, *self._key(), expected_revision),
            )
            return revision + 1

        return self._operation(actor, request_id, "activate", version, [expected_revision], write)

    def withdraw(self, actor: Principal, *, request_id: str, version: str) -> None:
        """Permanently block new resolutions and retries of an unsafe version; retain its body."""

        self._check(actor)

        def write(connection: Database, revision: int) -> int:
            row = connection.execute(
                "SELECT 1 FROM prompt_versions WHERE application = %s AND environment = %s "
                "AND version = %s",
                (*self._key(), version),
            ).fetchone()

            if row is None:
                raise PromptUnavailableError("prompt version unavailable")

            return revision

        self._operation(actor, request_id, "withdraw", version, [], write)

    def _check(self, actor: Principal) -> None:
        if not self._authorize(actor, self.scope):
            raise PermissionError("prompt administration is not authorised")

    def _operation(
        self,
        actor: Principal,
        request_id: str,
        action: str,
        version: str,
        payload: list[Any],
        write: Callable[[Database, int], int],
    ) -> int:
        if not request_id or not version:
            raise ValueError("stable request ID and version are required")

        digest = fingerprint([actor.tenant_id, actor.user_id, action, version, payload])

        # Scope locking serializes publishers/CAS activation, while resolvers use
        # ordinary committed reads. No lock is held during model inference.
        for _ in range(3):
            try:
                with self._transaction(write=True) as connection:
                    self._write(
                        connection,
                        "INSERT INTO prompt_bindings (application, environment) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING",
                        self._key(),
                    )
                    binding = connection.execute(
                        "SELECT revision FROM prompt_bindings WHERE application = %s "
                        "AND environment = %s FOR UPDATE",
                        self._key(),
                    ).fetchone()
                    assert binding is not None
                    prior = connection.execute(
                        "SELECT fingerprint, revision FROM prompt_operations "
                        "WHERE application = %s "
                        "AND environment = %s AND request_id = %s",
                        (*self._key(), request_id),
                    ).fetchone()

                    if prior:
                        if prior["fingerprint"] != digest:
                            raise StateConflictError(
                                "prompt request ID reused with different content"
                            )

                        return prior["revision"]

                    revision = write(connection, binding["revision"])
                    self._write(
                        connection,
                        "INSERT INTO prompt_operations (application, environment, request_id, "
                        "fingerprint, action, version, actor_tenant, actor_user, revision) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            *self._key(),
                            request_id,
                            digest,
                            action,
                            version,
                            actor.tenant_id,
                            actor.user_id,
                            revision,
                        ),
                    )

                    if action == "withdraw":
                        self._write(
                            connection,
                            "INSERT INTO prompt_withdrawals VALUES (%s, %s, %s, %s)",
                            (*self._key(), version, request_id),
                        )

                self._fault("after_commit")
                return revision
            except psycopg.OperationalError, psycopg.errors.SerializationFailure:
                continue
            except UniqueViolation:
                raise StateConflictError("prompt version or operation already exists") from None

        raise CommitUnknownError("resolve the same prompt request ID on the primary")

    def _write(
        self, connection: Database, query: LiteralString, parameters: tuple[Any, ...]
    ) -> None:
        connection.execute(query, parameters)
        self._fault("after_write")

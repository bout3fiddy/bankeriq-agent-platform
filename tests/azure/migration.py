"""Run schema publication and scoped SQL role setup inside the private pilot network."""

import json
import os

from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_harness.prompts.resolution import PromptScope
from agent_platform.prompt_files.loader import load_catalog
from agent_runtime.hosting.access import Principal
from agent_runtime.storage.connections import PrimaryConnections
from agent_runtime.storage.postgres import PostgresStateStore, migrate
from agent_runtime.storage.prompts import PromptAdministration
from agent_runtime.workers.queue import PostgresQueue
from tests.azure.recovery import prepare_restore


def initialise(dsn: str, connections: PrimaryConnections) -> None:
    migrate(dsn, connections=connections)

    if generation := os.environ.get("RESTORE_GENERATION"):
        changed = prepare_restore(connections, generation)
        print("restore generation advanced:", changed, flush=True)

    store = PostgresStateStore(dsn, connections=connections)
    queue = PostgresQueue(store)

    # Only the migration identity changes registered capacity. Startup's
    # configure() continues rejecting silent changes from ordinary workers.
    with store._connect() as connection:
        for kind in ("model_prepare", "model_infer", "tool"):
            connection.execute(
                "INSERT INTO worker_limits VALUES (%s,8,5) ON CONFLICT (queue) "
                "DO UPDATE SET concurrency=8,tenant_concurrency=5",
                (kind,),
            )

    for kind in ("model_prepare", "model_infer", "tool"):
        queue.configure(kind, concurrency=8, tenant_concurrency=5)

    # Five independent chats for one operator can execute together. Backlog caps
    # are primary-side reservations shared by every API replica. Two accepted
    # turns per conversation preserve ordering while bounding accidental repeats.
    with store._connect() as connection:
        connection.execute(
            "UPDATE runtime_admission SET bounded=true,global_limit=64,"
            "tenant_limit=16,conversation_limit=2"
        )

    actor = Principal(os.environ["AZURE_TENANT_ID"], "migration")
    prompts = PromptAdministration(
        dsn,
        PromptScope("bankeriq", "pilot"),
        catalog=load_catalog(),
        authorize=lambda supplied, scope: supplied == actor,
        connections=connections,
    )
    catalog = load_catalog()
    selected = catalog.catalog.defaults.system
    version = f"{selected.id}@{selected.version}"
    prompts.publish_definition(
        actor, request_id="pilot-publish:" + version, validation_reference="group12"
    )
    active, revision = prompts.active_binding(actor)

    if active != version:

        # A changed application prompt and memory profile needs a drained release.
        # Operators stop old consumers through Terraform before this job runs;
        # readiness of a new API alone cannot establish consumer compatibility.
        # Keep pinned work recoverable on its old image when drain is incomplete.
        with store._connect() as connection:
            unfinished = connection.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running','waiting') LIMIT 1"
            ).fetchone()

        if unfinished is not None:
            raise RuntimeError("drain accepted runs before activating a changed prompt")

        prompts.activate(
            actor,
            request_id="pilot-activate:" + version,
            version=version,
            expected_revision=revision,
        )

    # Only the migration identity owns DDL and prompt publication. Runtime
    # identities get the tables needed for durable operation, without DELETE,
    # schema creation or prompt mutation. The pilot's data is entirely synthetic.
    with connections.connection() as connection:
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall()

        for role, identity in json.loads(os.environ["RUNTIME_IDENTITIES"]).items():
            if role == "migration":
                continue

            name = identity["name"]
            exists = connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)
            ).fetchone()

            if not exists:

                # Azure's Entra role functions live in the postgres database;
                # their roles are cluster-wide and then receive per-database grants.
                directory = PrimaryConnections(
                    make_conninfo(dsn, dbname="postgres"),
                    connection_type=connections.connection_type,
                )

                with directory.connection() as admin:
                    admin.execute(
                        "SELECT * FROM pgaadauth_create_principal_with_oid(%s,%s,"
                        "'service',false,false)",
                        (name, identity["principal_id"]),
                    )

            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(name))
            )

            for table in tables:
                table_name = table["tablename"]
                permissions = (
                    "SELECT"
                    if table_name.startswith("prompt_") or table_name == "schema_versions"
                    else "SELECT,INSERT,UPDATE"
                )
                connection.execute(
                    sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                        sql.SQL(permissions), sql.Identifier(table_name), sql.Identifier(name)
                    )
                )

            connection.execute(
                sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                    sql.Identifier(name)
                )
            )

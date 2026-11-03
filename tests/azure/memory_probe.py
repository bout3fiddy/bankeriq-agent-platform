"""Explicit operator export of one owned synthetic chat and its durable summaries.

Run inside the API container through authenticated exec. This is a transcript
export, separate from lifecycle telemetry. It reads one allowlisted user's scope;
optional cache eviction removes only that conversation's disposable history view.
"""

import argparse
import json
import os
from uuid import UUID

import certifi
from azure.identity import ManagedIdentityCredential
from psycopg.conninfo import make_conninfo

from agent_harness.runs.context import RunContext
from agent_platform.azure.connections import token_connection
from agent_runtime.sessions.cache import RedisSessions
from agent_runtime.sessions.service import SessionService
from agent_runtime.storage.connections import PrimaryConnections
from agent_runtime.storage.postgres import PostgresStateStore
from tests.azure.probe import redis_client


def capture(store, context):
    with store._connect() as connection:

        # _head verifies the owner and deletion state before any payload read.
        # Every following query retains that tenant/conversation boundary. Export
        # limits fail explicitly rather than silently truncating the transcript.
        head = store._head(connection, context)
        scope = store._scope(context)
        events = connection.execute(
            "SELECT sequence,run_id,payload FROM conversation_events "
            "WHERE tenant_id=%s AND conversation_id=%s ORDER BY sequence LIMIT 501",
            scope,
        ).fetchall()
        summaries = connection.execute(
            "SELECT version,covered_turn,covered_cursor,prompt_manifest,facts,partial_turn,"
            "retained_cursor,retirements "
            "FROM semantic_summaries WHERE tenant_id=%s AND conversation_id=%s "
            "ORDER BY version LIMIT 101",
            scope,
        ).fetchall()

        if len(events) > 500 or len(summaries) > 100:
            raise ValueError("synthetic transcript exceeds export bound")

        operations = connection.execute(
            "SELECT run_id,operation_id,attempts,result_version FROM semantic_operations "
            "WHERE tenant_id=%s AND conversation_id=%s ORDER BY target_cursor LIMIT 101",
            scope,
        ).fetchall()
        groups = connection.execute(
            "SELECT run_id,group_id,joined FROM tool_groups "
            "WHERE tenant_id=%s AND conversation_id=%s ORDER BY group_id LIMIT 101",
            scope,
        ).fetchall()
        jobs = connection.execute(
            "SELECT operation,status,count(*) AS jobs,max(attempt) AS max_attempt FROM jobs "
            "WHERE tenant_id=%s AND conversation_id=%s "
            "GROUP BY operation,status ORDER BY operation,status",
            scope,
        ).fetchall()

    return {
        "conversation": context.conversation_id,
        "cursor": head["cursor"],
        "events": events,
        "summaries": summaries,
        "operations": operations,
        "groups": groups,
        "jobs": jobs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--conversation", required=True, type=UUID)
    parser.add_argument("--run", required=True, type=UUID)
    parser.add_argument("--clear-view", action="store_true")
    args = parser.parse_args()

    if args.user not in json.loads(os.environ["ALLOWED_USERS"]):
        raise PermissionError("operator is outside the configured allowlist")

    context = RunContext(
        os.environ["AZURE_TENANT_ID"], args.user, str(args.conversation), str(args.run)
    )

    with ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"]) as credential:
        connections = PrimaryConnections(
            make_conninfo(
                host=os.environ["POSTGRES_HOST"],
                dbname="bankeriq",
                user=os.environ["POSTGRES_USER"],
                sslmode="verify-full",
                sslrootcert=certifi.where(),
                connect_timeout=10,
            ),
            connection_type=token_connection(
                lambda: (
                    credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token
                )
            ),
        )
        store = PostgresStateStore(connections.dsn, connections=connections)
        result = capture(store, context)

        if args.clear_view:
            client, _ = redis_client()
            cache = RedisSessions("", client=client)

            try:
                sessions = SessionService(store, cache)
                head = sessions.history.head(context)
                result["deleted_view_keys"] = client.delete(cache.key(head))
                result["rebuilt_cursor"] = sessions.read(context)["cursor"]
            finally:
                cache.close()

        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

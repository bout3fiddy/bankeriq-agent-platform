"""Run bounded synthetic diagnostics inside the deployed private network."""

import argparse
import ipaddress
import json
import os
import socket
import time
from urllib.parse import urlparse

import certifi
from azure.identity import ManagedIdentityCredential
from psycopg.conninfo import make_conninfo
from redis import Redis
from redis.auth.token_manager import RetryPolicy, TokenManagerConfig
from redis.credentials import StreamingCredentialProvider
from redis_entraid.cred_provider import create_from_managed_identity
from redis_entraid.identity_provider import ManagedIdentityIdType, ManagedIdentityType

from agent_harness.runs.context import RunContext
from agent_platform.azure.connections import token_connection
from agent_runtime.sessions.cache import RedisSessions
from agent_runtime.sessions.service import SessionService
from agent_runtime.storage.connections import PrimaryConnections
from agent_runtime.storage.postgres import PostgresStateStore


class CountedRefresh(StreamingCredentialProvider):
    """Observe the public streaming callback while retaining Redis's re-auth handler.

    The test asks for an early refresh to exercise acquisition and AUTH on a real
    cache connection. Azure may return its cached, still-valid token. This measures
    the refresh/re-auth path and does not claim a natural 24-hour token expiry soak.
    """

    def __init__(self, delegate):
        self.delegate = delegate
        self.refreshes = 0

    def get_credentials(self):
        return self.delegate.get_credentials()

    def on_next(self, callback):
        def renewed(credentials):
            self.refreshes += 1
            callback(credentials)

        self.delegate.on_next(renewed)

    def on_error(self, callback):
        self.delegate.on_error(callback)

    def is_streaming(self):
        return self.delegate.is_streaming()


def redis_client(early_refresh=False):
    refresh = TokenManagerConfig(
        0.00001 if early_refresh else 0.9,
        1000 if early_refresh else 60000,
        10000,
        RetryPolicy(3, 1000),
    )
    provider = CountedRefresh(
        create_from_managed_identity(
            ManagedIdentityType.USER_ASSIGNED,
            "https://redis.azure.com",
            ManagedIdentityIdType.CLIENT_ID,
            os.environ["AZURE_CLIENT_ID"],
            token_manager_config=refresh,
        )
    )
    client = Redis(
        host=os.environ["REDIS_HOST"],
        port=10000,
        ssl=True,
        protocol=3,
        credential_provider=provider,
        socket_timeout=3,
        socket_connect_timeout=3,
        max_connections=2,
    )
    return client, provider


def inspect_database(connections):
    with connections.connection() as connection:
        tls = connection.execute(
            "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
        ).fetchone()
        assert tls is not None and tls["ssl"]
        jobs = connection.execute(
            "SELECT operation,status,count(*) AS jobs,max(attempt) AS max_attempt "
            "FROM jobs GROUP BY operation,status ORDER BY operation,status"
        ).fetchall()
        groups = connection.execute(
            "SELECT count(*) AS groups,count(*) FILTER (WHERE joined) AS joined FROM tool_groups"
        ).fetchone()
        runs = connection.execute(
            "SELECT status,count(*) AS runs FROM runs GROUP BY status ORDER BY status"
        ).fetchall()
        grants = connection.execute(
            "SELECT has_schema_privilege(current_user,'public','CREATE') AS ddl,"
            "has_table_privilege(current_user,'prompt_versions','INSERT') AS prompt_write"
        ).fetchone()

        if os.environ["RUNTIME_ROLE"] != "migration":
            assert grants == {"ddl": False, "prompt_write": False}

    addresses = {}

    for service, host in {
        "postgres": os.environ["POSTGRES_HOST"],
        "redis": os.environ["REDIS_HOST"],
        "model": urlparse(os.environ["FOUNDRY_OPENAI_BASE_URL"]).hostname,
    }.items():
        resolved = {entry[4][0] for entry in socket.getaddrinfo(host, None)}
        assert resolved and all(ipaddress.ip_address(address).is_private for address in resolved)
        addresses[service] = sorted(resolved)

    return {
        "tls": True,
        "private_addresses": addresses,
        "jobs": jobs,
        "groups": groups,
        "runs": runs,
        "grants": grants,
    }


def check_redis(connections, *, reset):
    client, provider = redis_client(early_refresh=not reset)

    try:
        assert client.ping()

        if not reset:
            for _ in range(15):
                time.sleep(1)
                assert client.ping()

                if provider.refreshes >= 2:
                    break

            assert provider.refreshes >= 2
            return {"refresh_callbacks": provider.refreshes, "post_refresh_ping": True}

        store = PostgresStateStore(connections.dsn, connections=connections)
        cache = RedisSessions("", client=client)
        sessions = SessionService(store, cache)

        # All data in this deployment is reviewed synthetic input. Capture an
        # owned durable cursor, remove the disposable cache and rebuild that exact
        # view. No run, accepted input, prompt version or lease lives only in Redis.
        with connections.connection() as connection:
            row = connection.execute(
                "SELECT tenant_id,user_id,conversation_id,run_id FROM runs "
                "JOIN conversations USING (tenant_id,conversation_id) "
                "WHERE NOT deleted ORDER BY conversations.last_activity DESC LIMIT 1"
            ).fetchone()

        assert row is not None
        context = RunContext(**row)
        before = sessions.read(context)
        client.flushdb()
        after = sessions.read(context)
        assert (after["cursor"], after["recovery_id"]) == (before["cursor"], before["recovery_id"])
        return {"redis_flushed": True, "durable_cursor_rebuilt": after["cursor"]}
    finally:
        client.close()


def main(action):
    acquired = 0

    with ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"]) as credential:

        def token():
            nonlocal acquired
            acquired += 1
            return credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token

        connections = PrimaryConnections(
            make_conninfo(
                host=os.environ["POSTGRES_HOST"],
                dbname="bankeriq",
                user=os.environ["POSTGRES_USER"],
                sslmode="verify-full",
                sslrootcert=certifi.where(),
                connect_timeout=10,
            ),
            connection_type=token_connection(token),
        )
        result = inspect_database(connections)

        # Two fresh physical sessions each obtain a currently valid credential.
        # This explicitly tests reconnect authentication without persisting tokens.
        with connections.connection() as connection:
            assert connection.execute("SELECT 1 AS n").fetchone() == {"n": 1}

        assert acquired >= 2
        result["postgres_token_acquisitions"] = acquired

        if action in ("redis-reset", "refresh"):
            result.update(check_redis(connections, reset=action == "redis-reset"))

        print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["inspect", "redis-reset", "refresh"])
    main(parser.parse_args().action)

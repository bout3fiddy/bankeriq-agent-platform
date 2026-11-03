"""Signal only the running synthetic tool entry point after observing an active lease."""

import json
import os
import signal
import time
from pathlib import Path

import certifi
import psycopg
from azure.identity import ManagedIdentityCredential

# Run through Container Apps exec in the disposable tool container. The process
# guard prevents signalling an unrelated PID 1 when this file is invoked elsewhere.
if os.environ.get("RUNTIME_ROLE") != "tool":
    raise ValueError("drain probe requires the synthetic tool container")

if b"tests.azure.pilot" not in Path("/proc/1/cmdline").read_bytes():
    raise ValueError("PID 1 is not the synthetic runtime entry point")

with (
    ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"]) as credential,
    psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        dbname="bankeriq",
        user=os.environ["POSTGRES_USER"],
        password=credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token,
        sslmode="verify-full",
        sslrootcert=certifi.where(),
        connect_timeout=10,
        autocommit=True,
    ) as connection,
):
    print("drain probe armed", flush=True)

    # A single tool replica owns the next observed lease. Signal while the
    # synthetic 40-second executor is active, before waiting for completion.
    for _ in range(120):
        row = connection.execute(
            "SELECT count(*) FROM jobs WHERE operation='tool' AND status='leased'"
        ).fetchone()
        assert row is not None

        if row[0]:
            print(json.dumps({"active_tool_jobs": row[0], "signal": "SIGTERM"}), flush=True)
            os.kill(1, signal.SIGTERM)
            break

        time.sleep(0.5)
    else:
        raise AssertionError("no active tool lease observed before probe deadline")

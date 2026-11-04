"""Run the reviewed live memory suite with isolated local PostgreSQL state."""

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx2
import psycopg
from openai import AsyncOpenAI
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent_harness.prompts.resolution import PromptScope
from agent_platform.prompt_files.loader import load_catalog
from agent_runtime.hosting.access import Principal
from agent_runtime.providers.foundry.model import AsyncFoundryModelClient
from agent_runtime.storage.connections import PrimaryConnections
from agent_runtime.storage.postgres import PostgresStateStore, migrate
from agent_runtime.storage.prompts import PostgresPromptResolver, PromptAdministration
from agent_runtime.workers.queue import PostgresQueue
from tests.azure.exec_transport import LocalRelayTransport
from tests.quality.memory import MeasuredModel, evaluate, quality_passes


async def main(args):
    primary = os.environ["BANKERIQ_TEST_POSTGRES_DSN"]

    if conninfo_to_dict(primary).get("host") not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("quality fixtures require a disposable local primary")

    if args.output.exists():
        raise ValueError("preserve previous evidence and choose a new output")

    fixture = args.cases_file.read_bytes()
    cases = json.loads(fixture)

    if args.case:
        cases = [case for case in cases if case["id"] == args.case]

        if not cases:
            raise ValueError("unknown memory case")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    schema = "quality_" + uuid4().hex

    with psycopg.connect(primary) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    connections = None

    try:
        dsn = make_conninfo(primary, options=f"-c search_path={schema}", application_name=schema)
        migrate(dsn)
        connections = PrimaryConnections(dsn, size=4)
        store = PostgresStateStore(dsn, connections=connections)
        queue = PostgresQueue(store)

        for kind in ("model_prepare", "model_infer", "tool"):
            queue.configure(kind, concurrency=8, tenant_concurrency=5)

        scope = PromptScope("bankeriq", "pilot")
        actor = Principal("quality-tenant", "quality-operator")
        catalog = load_catalog()
        administration = PromptAdministration(
            dsn,
            scope,
            catalog=catalog,
            authorize=lambda supplied, _: supplied == actor,
            connections=connections,
        )
        administration.publish_definition(
            actor, request_id="quality-publish", validation_reference="12"
        )
        selected = catalog.catalog.defaults.system
        administration.activate(
            actor,
            request_id="quality-activate",
            version=f"{selected.id}@{selected.version}",
            expected_revision=0,
        )
        prompts = PostgresPromptResolver(dsn, scope, catalog=catalog, connections=connections)

        # Synthetic histories live only in this owned schema. Every evaluated summary
        # and final answer uses this checkout's pinned prompt/profile. Verify the
        # deployed digest separately before claiming parity with the hosted pilot.
        # The relay owns private-network authentication; no credential enters evidence.
        observations = []
        transport_observations = []
        async with (
            httpx2.AsyncClient(
                transport=LocalRelayTransport(args.socket, observer=transport_observations.append)
            ) as http,
            AsyncOpenAI(
                api_key="relay-only",
                base_url="https://relay.invalid/v1/",
                http_client=http,
                max_retries=0,
            ) as sdk,
        ):
            provider = AsyncFoundryModelClient(
                sdk,
                deployment="coordinator",
                observer=lambda item: observations.append(asdict(item)),
            )
            model = MeasuredModel(
                provider,
                observations,
                input_limit=args.budget_input_tokens,
                output_limit=args.budget_output_tokens,
                call_limit=args.budget_calls,
                dispatch_interval=args.dispatch_interval,
                transport_observations=transport_observations,
            )
            record = await evaluate(
                store,
                prompts,
                model,
                cases,
                args.output,
                fixture_digest=hashlib.sha256(fixture).hexdigest(),
            )

        return 0 if quality_passes(record, cases) else 1
    finally:
        if connections is not None:
            connections.close()

        with psycopg.connect(primary) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case")
    parser.add_argument(
        "--cases-file", type=Path, default=Path("performance/workloads/memory_quality_cases.json")
    )
    parser.add_argument("--budget-calls", type=int, required=True)
    parser.add_argument("--budget-input-tokens", type=int, required=True)
    parser.add_argument("--budget-output-tokens", type=int, required=True)
    parser.add_argument("--dispatch-interval", type=int, choices=[0, 7, 10], default=10)
    raise SystemExit(asyncio.run(main(parser.parse_args())))

"""Observe provider throttling and existing recovery controls on the disposable coordinator."""

import argparse
import json
import os
import time

import certifi
from azure.identity import ManagedIdentityCredential, get_bearer_token_provider
from openai import OpenAI, RateLimitError
from psycopg.conninfo import make_conninfo

from agent_runtime.prompts.resolution import PromptScope
from agent_runtime.runs.context import RunContext
from bankeriq_agent_platform.azure.connections import token_connection
from bankeriq_agent_platform.storage.connections import PrimaryConnections
from bankeriq_agent_platform.storage.postgres import PostgresStateStore
from bankeriq_agent_platform.storage.prompts import PostgresPromptResolver
from bankeriq_agent_platform.workers.controls import ControlRequest, RecoveryActor, RecoveryControls
from tests.azure.pilot import Access, NoModel, composition


def provider_limit(credential):
    with OpenAI(
        base_url=os.environ["FOUNDRY_OPENAI_BASE_URL"],
        max_retries=0,
        timeout=15,
        api_key=get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        ),
    ) as client:
        try:
            client.chat.completions.create(
                model="coordinator",
                max_completion_tokens=16,
                messages=[{"role": "user", "content": "synthetic " * 4096 + "Reply OK."}],
            )
        except RateLimitError as error:
            assert error.status_code == 429
            return {"provider_http_status": 429, "sdk_retries": 0}

    raise AssertionError("the intentionally constrained provider did not return 429")


def inspect_stopped(credential):
    tenant = os.environ["AZURE_TENANT_ID"]
    users = frozenset(json.loads(os.environ["ALLOWED_USERS"]))
    source = PrimaryConnections(
        make_conninfo(
            host=os.environ["POSTGRES_HOST"],
            dbname="bankeriq",
            user=os.environ["POSTGRES_USER"],
            sslmode="verify-full",
            sslrootcert=certifi.where(),
            connect_timeout=10,
        ),
        connection_type=token_connection(
            lambda: credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token
        ),
    )
    store = PostgresStateStore(source.dsn, connections=source)
    prompts = PostgresPromptResolver(
        source.dsn, PromptScope("bankeriq", "pilot"), connections=source
    )
    runtime, handlers = composition(store, prompts, NoModel(), Access(tenant, users))
    actor = RecoveryActor(tenant, next(iter(users)))
    controls = RecoveryControls(
        runtime.queue,
        handlers,
        authorize=lambda supplied, context, action: supplied == actor and context.user_id in users,
    )

    # A 429 is handled by the existing bounded worker failure path. Exhaustion
    # retains its unknown-outcome flag; the probe cannot reset that budget or
    # invent a provider receipt. Scoped inspection remains available to operators.
    with source.connection() as connection:
        row = connection.execute(
            "SELECT j.job_id,j.tenant_id,j.conversation_id,j.run_id,c.user_id FROM jobs j "
            "JOIN conversations c USING (tenant_id,conversation_id) "
            "WHERE j.tenant_id=%s AND c.user_id=ANY(%s) "
            "AND j.operation='model_infer' AND j.status='stopped' LIMIT 1",
            (tenant, list(users)),
        ).fetchone()

    assert row is not None, "throttled job has not reached its bounded stop"
    job_id = row.pop("job_id")
    context = RunContext(**row)
    inspected = controls.inspect(actor, context, job_id)
    assert inspected.attempts == inspected.attempt_limit == 3
    assert inspected.unknown_effect and not inspected.allowed_actions
    request = ControlRequest(
        request_id="throttle-retry-" + context.run_id,
        action="retry",
        expected_revision=inspected.revision,
        recovery_id=inspected.recovery_id,
    )
    receipt = controls.apply(actor, context, job_id, request)
    assert receipt.decision == "ineligible"
    assert controls.apply(actor, context, job_id, request) == receipt

    try:
        controls.inspect(RecoveryActor("other-tenant", actor.actor_id), context, job_id)
    except PermissionError:
        pass
    else:
        raise AssertionError("cross-tenant inspection was accepted")

    time.sleep(3)
    assert controls.inspect(actor, context, job_id).attempts == 3
    return {
        "attempts": 3,
        "unknown_outcome_retained": True,
        "retry_decision": receipt.decision,
        "duplicate_control_idempotent": True,
        "cross_tenant_inspection_denied": True,
    }


def main(action):
    if os.environ.get("RUNTIME_ROLE") != "coordinator":
        raise ValueError("throttle probe requires the synthetic coordinator identity")

    with ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"]) as credential:
        result = provider_limit(credential) if action == "provider" else inspect_stopped(credential)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["provider", "inspect"])
    main(parser.parse_args().action)

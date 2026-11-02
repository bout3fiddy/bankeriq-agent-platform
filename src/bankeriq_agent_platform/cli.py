"""Launch the synthetic runtime or validate it with a mock or live Foundry model."""

import argparse
import asyncio
import json
import os
import secrets
import sys
from contextlib import nullcontext
from pathlib import Path

from pydantic import TypeAdapter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve", "validate"])
    parser.add_argument("--provider", choices=["mock", "foundry"], default="mock")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--config", type=Path, help="JSON object containing environment settings")
    parser.add_argument(
        "--transcript", type=Path, help="Write validation history to a new Markdown file"
    )
    arguments = parser.parse_args()

    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")

    if arguments.transcript is not None and arguments.command != "validate":
        parser.error("--transcript requires validate")

    from .demo.application import open_demo

    token = os.environ.get("BANKERIQ_DEMO_TOKEN", "")

    if arguments.command == "validate":
        token = secrets.token_urlsafe(32)

    try:
        configuration = dict(os.environ)

        if arguments.config is not None:
            configuration.update(
                TypeAdapter(dict[str, str]).validate_json(
                    arguments.config.read_bytes(), strict=True
                )
            )

        # Exclusive creation keeps previous test evidence intact. Open the file
        # before the runtime so an invalid destination fails before live inference.
        with (
            (
                arguments.transcript.open("x", encoding="utf-8")
                if arguments.transcript is not None
                else nullcontext()
            ) as transcript_file,
            open_demo(
                configuration,
                provider=arguments.provider,
                token=token,
                port=arguments.port,
                host=arguments.host,
            ) as (server, dependencies),
        ):
            if arguments.command == "serve":
                print(f"Synthetic runtime: http://{arguments.host}:{arguments.port}", flush=True)
                server.run()
            else:
                from .demo.validation import validate

                model = "synthetic" if arguments.provider == "mock" else configuration["MODEL_NAME"]
                result = asyncio.run(
                    validate(
                        server,
                        dependencies,
                        token,
                        transcript_file=transcript_file,
                        model_label=f"{arguments.provider}/{model}",
                    )
                )
                print(json.dumps(result))
    except (KeyError, ValueError, OSError) as error:

        # Startup errors can contain credential/configuration values. Report
        # only the exception type and point to the documented required settings.
        print(
            f"Startup configuration failed ({type(error).__name__}); see the demo README.",
            file=sys.stderr,
        )
        return 2
    except RuntimeError as error:
        print(
            f"Validation/runtime failed ({type(error).__name__}); "
            "no successful validation recorded.",
            file=sys.stderr,
        )
        return 1

    return 0

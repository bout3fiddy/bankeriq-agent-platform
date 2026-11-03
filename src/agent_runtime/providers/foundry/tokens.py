"""Local token accounting for the supported text-only Chat Completions profile."""

import json

import tiktoken

from agent_harness.coordinator.model import ModelRequest
from agent_runtime.providers.foundry.messages import encode_json, encode_messages


class FoundryTokenCounter:
    """Count encoded text plus conservative message/schema framing headroom.

    Supply the actual model name, e.g. gpt-4o-2024-08-06, rather than an Azure
    deployment alias. Unknown model encodings fail at startup. This is a local
    admission estimate, not provider billing usage: reserve framing headroom and
    configure a window below the deployed context limit. Text token counts use
    the model's BPE tokenizer, including non-ASCII and special-token-like input.
    """

    def __init__(self, model: str) -> None:
        self.encoding = tiktoken.encoding_for_model(model)

    def __call__(self, request: ModelRequest) -> int:
        messages = encode_messages(request.history)
        messages.insert(0, {"role": "system", "content": request.instructions})
        tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": json.loads(encode_json(tool.argument_schema)),
                    "strict": False,
                },
            }
            for tool in request.tools
        ]
        value = {"messages": messages, "tools": tools}

        if request.output_schema is not None:
            value["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "runtime_output",
                    "strict": True,
                    "schema": json.loads(encode_json(request.output_schema)),
                },
            }

        text = json.dumps(value, ensure_ascii=False)
        return (
            len(self.encoding.encode(text, disallowed_special=()))
            + 256
            + 32 * (len(messages) + len(tools))
        )

"""Deterministic source selection shared by queued acceptance fixtures."""

import asyncio
import json
import os
import re
from pathlib import Path

from agent_harness.conversations.messages import Message
from agent_harness.coordinator.model import ModelReply, ModelRequest


class Extractor:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)

        if marker := os.environ.get("SEMANTIC_DISPATCH_MARKER"):
            Path(marker).write_text("summary dispatch reserved")
            await asyncio.Event().wait()

        message = request.history[0]
        assert isinstance(message, Message)
        sources = json.loads(message.text.split("\n", 1)[1])
        entries = []

        for source in sources:
            matches = re.findall(r"(?:Decision|Open question): [^.]+\.", source["text"])

            if not matches and source["label"] in ("decision", "open_question"):
                matches = [source["text"]]

            for quote in matches:
                entries.append(
                    {
                        "candidate": source["candidate"],
                        "label": "open_question" if "question" in quote else "decision",
                    }
                )

        if not entries and sources:
            entries.append(
                {
                    "candidate": sources[-1]["candidate"],
                    "label": "fact",
                }
            )

        return ModelReply(answer=json.dumps({"entries": entries}))

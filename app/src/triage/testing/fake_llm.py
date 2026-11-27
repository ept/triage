"""A scripted stand-in for the local LLM server, for tests and for trying out the interface.

It plugs into the fake gateway (``fake.llm_handler = scripted.handler``) and answers each chat
completion request with the next reply from its script, in the server's streaming format.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from triage.testing.fake_gateway import FakeResponse


@dataclass(frozen=True, slots=True)
class Call:
    """A tool call that the scripted model makes."""

    name: str
    arguments: Mapping[str, Any] | str = field(default_factory=dict)


Reply = str | Call | Sequence[Call] | Callable[[Mapping[str, Any]], "Reply"]


def _events(reply: str | Sequence[Call]) -> list[dict[str, Any]]:
    if isinstance(reply, str):
        # Split the text, so that clients see more than one piece.
        middle = len(reply) // 2
        pieces = [p for p in (reply[:middle], reply[middle:]) if p]
        events: list[dict[str, Any]] = [
            {"choices": [{"delta": {"content": piece}}]} for piece in pieces
        ]
        events.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        return events
    calls = []
    for index, call in enumerate(reply):
        arguments = (
            call.arguments if isinstance(call.arguments, str) else json.dumps(call.arguments)
        )
        calls.append(
            {
                "index": index,
                "id": f"call_{index}",
                "type": "function",
                "function": {"name": call.name, "arguments": arguments},
            }
        )
    return [
        {"choices": [{"delta": {"tool_calls": calls}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]


class ScriptedLlm:
    """Answers chat completion requests from a list of prepared replies.

    A reply is a text, a :class:`Call` (or several), or a function that is given the request
    body and returns one of those. When the script runs out, ``default`` is used.
    """

    def __init__(self, replies: Sequence[Reply] = (), *, default: Reply = "(no more replies)"):
        self.replies: list[Reply] = list(replies)
        self.default = default
        self.requests: list[dict[str, Any]] = []
        self.model = "scripted-test-model"
        self.fail_with: tuple[int, str] | None = None

    def handler(self, method: str, path: str, body: bytes) -> FakeResponse:
        if method == "GET" and path == "/v1/models":
            return FakeResponse.json({"data": [{"id": self.model}]})
        if method == "POST" and path == "/v1/chat/completions":
            if self.fail_with is not None:
                status, message = self.fail_with
                return FakeResponse.json({"error": {"message": message}}, status)
            request = json.loads(body)
            self.requests.append(request)
            reply = self.replies.pop(0) if self.replies else self.default
            while callable(reply):
                reply = reply(request)
            if isinstance(reply, Call):
                reply = [reply]
            lines = [f"data: {json.dumps(event)}\n\n" for event in _events(reply)]
            lines.append("data: [DONE]\n\n")
            return FakeResponse(200, {"content-type": "text/event-stream"}, "".join(lines).encode())
        return FakeResponse.json({"error": {"message": "not found"}}, 404)

    def last_messages(self) -> list[dict[str, Any]]:
        """The messages of the most recent request."""
        return list(self.requests[-1]["messages"])

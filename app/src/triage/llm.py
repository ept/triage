"""Client for the local LLM.

The model runs on the user's machine, outside the email sandbox, and is reached through the
gateway's relay (``/v1/llm/...``), which speaks the OpenAI-compatible chat completions API of
servers such as llama.cpp's ``llama-server``. Email content may be sent to this model, and to
no other.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from triage.gateway import DEFAULT_GATEWAY_URL

Message = Mapping[str, Any]
Tool = Mapping[str, Any]


class LlmError(Exception):
    """The model server answered with an error, or with something unintelligible."""


class LlmUnavailableError(LlmError):
    """The model server cannot be reached, or none is configured."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    """The arguments as the model wrote them: JSON text, not yet validated."""


@dataclass(frozen=True, slots=True)
class ChatResult:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None


@dataclass(slots=True)
class _PartialCall:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


class _ThinkFilter:
    """Removes ``<think>...</think>`` sections, which some models write into their answer."""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._thinking = False

    def feed(self, text: str) -> str:
        self._buffer += text
        out: list[str] = []
        while self._buffer:
            marker = self.CLOSE if self._thinking else self.OPEN
            index = self._buffer.find(marker)
            if index >= 0:
                if not self._thinking:
                    out.append(self._buffer[:index])
                self._buffer = self._buffer[index + len(marker) :]
                self._thinking = not self._thinking
                continue
            # Keep back a tail that could be the beginning of a marker.
            keep = 0
            for length in range(min(len(marker) - 1, len(self._buffer)), 0, -1):
                if marker.startswith(self._buffer[-length:]):
                    keep = length
                    break
            emit, self._buffer = (
                self._buffer[: len(self._buffer) - keep],
                self._buffer[-keep:] if keep else "",
            )
            if not self._thinking:
                out.append(emit)
            break
        return "".join(out)

    def finish(self) -> str:
        rest, self._buffer = self._buffer, ""
        return "" if self._thinking else rest


class LlmClient:
    """Synchronous client for the chat completions API behind the gateway's relay."""

    def __init__(
        self,
        gateway_url: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        model: str | None = None,
        timeout: float = 600.0,
    ):
        if gateway_url is None:
            gateway_url = os.environ.get("TRIAGE_GATEWAY_URL", DEFAULT_GATEWAY_URL)
        self._http = httpx.Client(
            base_url=gateway_url.rstrip("/") + "/v1/llm",
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=10.0),
            trust_env=False,
            follow_redirects=False,
        )
        self._configured_model = model
        self._model: str | None = model
        self._low_effort_refused = False
        self._lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    # -- availability --------------------------------------------------------------------

    def model(self) -> str:
        """The name of the model that the server runs. Raises if the server is unavailable."""
        with self._lock:
            if self._model is not None:
                return self._model
        try:
            response = self._http.get("/v1/models", timeout=10.0)
        except httpx.HTTPError as exc:
            raise LlmUnavailableError(
                f"the local model cannot be reached: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise LlmUnavailableError(_error_text(response))
        try:
            name = str(response.json()["data"][0]["id"])
        except (ValueError, LookupError, TypeError) as exc:
            raise LlmError("the local model server did not list a model") from exc
        with self._lock:
            self._model = name
        return name

    def forget_model(self) -> None:
        """Ask the server again next time (it may have been restarted with another model)."""
        with self._lock:
            self._model = self._configured_model

    # -- chat ----------------------------------------------------------------------------

    def stream_chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[Tool] = (),
        temperature: float = 0.2,
        max_tokens: int = 1500,
        thinking: int | None = None,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Iterator[str | ChatResult]:
        """Run one completion. Yields pieces of text as they arrive, then the ChatResult.

        ``max_tokens`` is the room for the answer. Many models think before they answer, and
        the server counts that against the same limit, so a model that thinks at length would
        return nothing at all. ``thinking`` is therefore a separate allowance, in tokens, that
        the server is asked to enforce (0: do not think); without it the model thinks as much
        as it likes, at the expense of the answer.
        """
        body: dict[str, Any] = {
            "model": self.model(),
            "messages": list(messages),
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens + (thinking or 0),
        }
        if thinking is not None:
            # Both are options of llama.cpp's server. The budget is enforced for any model;
            # the effort is a hint that only some models' templates understand.
            body["reasoning_budget_tokens"] = thinking
            effort = "none" if thinking == 0 else "low"
            if effort == "none" or not self._low_effort_refused:
                body["reasoning_effort"] = effort
        if tools:
            body["tools"] = list(tools)
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": dict(json_schema)},
            }
        response = self._send(body)
        if (
            response.status_code == 400
            and body.get("reasoning_effort") == "low"
            and "reasoning_effort" in _error_text(response)
        ):
            # This model's template knows no such level. Do without, from now on.
            response.close()
            self._low_effort_refused = True
            del body["reasoning_effort"]
            response = self._send(body)
        try:
            if response.status_code != 200:
                response.read()
                self.forget_model()
                error = _error_text(response)
                if response.status_code in (502, 503, 504):
                    raise LlmUnavailableError(error)
                raise LlmError(error)
            yield from self._read_stream(response)
        except httpx.HTTPError as exc:
            raise LlmUnavailableError(
                f"the connection to the local model broke: {type(exc).__name__}"
            ) from exc
        finally:
            response.close()

    def _send(self, body: Mapping[str, Any]) -> httpx.Response:
        request = self._http.build_request("POST", "/v1/chat/completions", json=body)
        try:
            response = self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            self.forget_model()
            raise LlmUnavailableError(
                f"the local model cannot be reached: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            try:
                response.read()
            except httpx.HTTPError as exc:
                response.close()
                raise LlmUnavailableError(
                    f"the connection to the local model broke: {type(exc).__name__}"
                ) from exc
        return response

    def _read_stream(self, response: httpx.Response) -> Iterator[str | ChatResult]:
        text: list[str] = []
        calls: dict[int, _PartialCall] = {}
        finish_reason: str | None = None
        think = _ThinkFilter()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if isinstance(chunk, dict) and "error" in chunk:
                raise LlmError(_describe_error(chunk["error"]))
            choices = chunk.get("choices") if isinstance(chunk, dict) else None
            if not choices:
                continue
            choice = choices[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                visible = think.feed(content)
                if visible:
                    text.append(visible)
                    yield visible
            for call in delta.get("tool_calls") or ():
                partial = calls.setdefault(int(call.get("index", 0)), _PartialCall())
                partial.id = call.get("id") or partial.id
                function = call.get("function") or {}
                partial.name = function.get("name") or partial.name
                if isinstance(function.get("arguments"), str):
                    partial.arguments.append(function["arguments"])
        rest = think.finish()
        if rest:
            text.append(rest)
            yield rest
        tool_calls = tuple(
            ToolCall(id=c.id or f"call_{index}", name=c.name, arguments="".join(c.arguments))
            for index, c in sorted(calls.items())
            if c.name
        )
        yield ChatResult("".join(text).strip(), tool_calls, finish_reason)

    def chat(self, messages: Sequence[Message], **options: Any) -> ChatResult:
        """Run one completion and return the whole result."""
        result: ChatResult | None = None
        for piece in self.stream_chat(messages, **options):
            if isinstance(piece, ChatResult):
                result = piece
        if result is None:
            raise LlmError("the local model sent no answer")
        return result


def _describe_error(error: object) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return f"the local model reported an error: {message[:300]}"
    return "the local model reported an error"


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"the local model server answered with status {response.status_code}"
    if isinstance(payload, dict) and "error" in payload:
        return _describe_error(payload["error"])
    return f"the local model server answered with status {response.status_code}"

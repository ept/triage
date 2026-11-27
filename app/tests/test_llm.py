from __future__ import annotations

import json

import pytest

from triage.llm import ChatResult, LlmClient, LlmError, LlmUnavailableError, _ThinkFilter
from triage.testing.fake_gateway import FakeGateway, FakeResponse
from triage.testing.fake_llm import Call, ScriptedLlm


def client_for(fake: FakeGateway, scripted: ScriptedLlm | None) -> LlmClient:
    fake.llm_handler = scripted.handler if scripted else None
    return LlmClient("http://gateway.test", transport=fake.transport())


def test_text_is_streamed_and_collected(fake: FakeGateway) -> None:
    scripted = ScriptedLlm(["Hello there, Robin."])
    llm = client_for(fake, scripted)
    pieces = list(llm.stream_chat([{"role": "user", "content": "Hi"}]))
    assert pieces[:-1] == ["Hello the", "re, Robin."]
    assert pieces[-1] == ChatResult("Hello there, Robin.", (), "stop")
    request = scripted.requests[0]
    assert request["model"] == "scripted-test-model"
    assert request["stream"] is True
    assert "tools" not in request
    assert llm.model() == "scripted-test-model"


def test_tool_calls_are_assembled(fake: FakeGateway) -> None:
    scripted = ScriptedLlm([[Call("search_mail", {"query": "invoice"}), Call("list_folders")]])
    llm = client_for(fake, scripted)
    tools = [{"type": "function", "function": {"name": "search_mail", "parameters": {}}}]
    result = llm.chat([{"role": "user", "content": "find it"}], tools=tools)
    assert result.text == ""
    assert [c.name for c in result.tool_calls] == ["search_mail", "list_folders"]
    assert json.loads(result.tool_calls[0].arguments) == {"query": "invoice"}
    assert result.finish_reason == "tool_calls"
    assert scripted.requests[0]["tools"] == tools


def test_arguments_arriving_in_fragments(fake: FakeGateway) -> None:
    def handler(method: str, path: str, body: bytes) -> FakeResponse:
        if path == "/v1/models":
            return FakeResponse.json({"data": [{"id": "m"}]})
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "read_message", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"id": '}}]}}
                ]
            },
            {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"m1"}'}}]}}
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        text = (
            "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + ": comment\n\ndata: [DONE]\n\n"
        )
        return FakeResponse(200, {"content-type": "text/event-stream"}, text.encode())

    fake.llm_handler = handler
    llm = LlmClient("http://gateway.test", transport=fake.transport())
    result = llm.chat([{"role": "user", "content": "x"}])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "c1"
    assert json.loads(result.tool_calls[0].arguments) == {"id": "m1"}


def test_json_schema_is_requested(fake: FakeGateway) -> None:
    scripted = ScriptedLlm(['{"ok": true}'])
    llm = client_for(fake, scripted)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    assert llm.chat([{"role": "user", "content": "x"}], json_schema=schema).text == '{"ok": true}'
    assert scripted.requests[0]["response_format"]["json_schema"]["schema"] == schema


def test_unavailable_and_failing_servers(fake: FakeGateway) -> None:
    llm = client_for(fake, None)
    with pytest.raises(LlmUnavailableError):
        llm.model()
    with pytest.raises(LlmUnavailableError):
        llm.chat([{"role": "user", "content": "x"}])

    scripted = ScriptedLlm(["fine"])
    llm = client_for(fake, scripted)
    assert llm.chat([{"role": "user", "content": "x"}]).text == "fine"
    scripted.fail_with = (500, "out of memory")
    with pytest.raises(LlmError, match="out of memory"):
        llm.chat([{"role": "user", "content": "x"}])
    scripted.fail_with = (503, "loading model")
    with pytest.raises(LlmUnavailableError):
        llm.chat([{"role": "user", "content": "x"}])


def test_errors_inside_the_stream(fake: FakeGateway) -> None:
    def handler(method: str, path: str, body: bytes) -> FakeResponse:
        if path == "/v1/models":
            return FakeResponse.json({"data": [{"id": "m"}]})
        text = 'data: {"error": {"message": "context size exceeded"}}\n\n'
        return FakeResponse(200, {"content-type": "text/event-stream"}, text.encode())

    fake.llm_handler = handler
    llm = LlmClient("http://gateway.test", transport=fake.transport())
    with pytest.raises(LlmError, match="context size exceeded"):
        llm.chat([{"role": "user", "content": "x"}])


@pytest.mark.parametrize(
    ("pieces", "expected"),
    [
        (["plain text"], "plain text"),
        (["<think>secret</think>answer"], "answer"),
        (["<thi", "nk>sec", "ret</th", "ink>ans", "wer"], "answer"),
        (["a <b> c < d"], "a <b> c < d"),
        (["before<think>x</think>middle<think>y</think>after"], "beforemiddleafter"),
        (["unfinished <think>never closed"], "unfinished "),
        (["ends with <thin"], "ends with <thin"),
    ],
)
def test_think_sections_are_removed(pieces: list[str], expected: str) -> None:
    think = _ThinkFilter()
    out = "".join(think.feed(piece) for piece in pieces) + think.finish()
    assert out == expected

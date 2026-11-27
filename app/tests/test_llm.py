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


def test_thinking_gets_an_allowance_of_its_own(fake: FakeGateway) -> None:
    scripted = ScriptedLlm()
    llm = client_for(fake, scripted)
    user = [{"role": "user", "content": "Hi"}]

    # Without an allowance, nothing about thinking is said: the model does as it likes.
    llm.chat(user, max_tokens=100)
    assert scripted.requests[-1]["max_tokens"] == 100
    assert "reasoning_budget_tokens" not in scripted.requests[-1]
    assert "reasoning_effort" not in scripted.requests[-1]

    # The allowance comes on top of the room for the answer, and the server enforces it.
    llm.chat(user, max_tokens=100, thinking=400)
    assert scripted.requests[-1]["max_tokens"] == 500
    assert scripted.requests[-1]["reasoning_budget_tokens"] == 400
    assert scripted.requests[-1]["reasoning_effort"] == "low"

    llm.chat(user, max_tokens=100, thinking=0)
    assert scripted.requests[-1]["max_tokens"] == 100
    assert scripted.requests[-1]["reasoning_budget_tokens"] == 0
    assert scripted.requests[-1]["reasoning_effort"] == "none"


def test_a_model_without_effort_levels_is_asked_again_without(fake: FakeGateway) -> None:
    scripted = ScriptedLlm(["first", "second"])
    refused: list[dict[str, object]] = []

    def handler(method: str, path: str, body: bytes) -> FakeResponse:
        request = json.loads(body) if body else {}
        if request.get("reasoning_effort") == "low":
            refused.append(request)
            message = 'reasoning_effort "low" is not supported by the chat template'
            return FakeResponse.json({"error": {"message": message}}, 400)
        return scripted.handler(method, path, body)

    fake.llm_handler = handler
    llm = LlmClient("http://gateway.test", transport=fake.transport())
    user = [{"role": "user", "content": "Hi"}]
    assert llm.chat(user, thinking=200).text == "first"
    assert len(refused) == 1
    assert scripted.requests[-1]["reasoning_budget_tokens"] == 200, "the budget still applies"
    # It is not tried again.
    assert llm.chat(user, thinking=200).text == "second"
    assert len(refused) == 1

    # Any other refusal is an error as before.
    fake.llm_handler = lambda method, path, body: (
        scripted.handler(method, path, body)
        if method == "GET"
        else FakeResponse.json({"error": {"message": "the request is too long"}}, 400)
    )
    with pytest.raises(LlmError, match="too long"):
        LlmClient("http://gateway.test", transport=fake.transport()).chat(user, thinking=200)


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

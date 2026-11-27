"""Requests that concern many messages, and the limits on a single turn of the assistant."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from test_agent import Setup, kinds

from triage import agent as agent_module
from triage.agent import OUT_OF_STEPS, REPEATED, STOPPED, WRAP_UP, fit_context
from triage.gateway import GatewayClient
from triage.store import Store
from triage.testing.fake_gateway import FakeGateway
from triage.testing.fake_llm import Call, Reply

REQUEST = "Flag every internship request from Cambridge, and decline the others."
MATCHING = "requests for an internship"
INSTRUCTION = "If the sender is at Cambridge, flag the message. Otherwise draft a reply declining."


def student(name: str) -> tuple[str, str]:
    return (name, f"{name.lower().replace(' ', '.')}@students.example")


@pytest.fixture
def setup(
    tmp_path: Path, fake: FakeGateway, client: GatewayClient, store: Store
) -> Iterator[Setup]:
    env = Setup(tmp_path, fake, client, store)
    env.deliver(
        sender=student("Priya Nair"),
        subject="Summer internship",
        text="I study at the University of Cambridge. May I intern with you?\n",
    )
    env.deliver(
        sender=student("Dana Whitlock"),
        subject="Internship request",
        text="I study at Northfield College. May I intern with you?\n",
    )
    env.deliver(
        sender=student("Tom Zielinski"),
        subject="Placement",
        text="I study at Riverside College and need a placement.\n",
    )
    for number in range(5):
        env.deliver(subject=f"Minutes {number}", text=f"Minutes of meeting {number}.\n")
    env.sync()
    yield env
    env.chats.close()


def stage(request: Mapping[str, Any]) -> str:
    """Which of the three kinds of request to the model this is."""
    system = request["messages"][0]["content"]
    if system.startswith("You help to pick"):
        return "screen"
    return "one" if "one at a time" in system else "main"


def handles(text: str, word: str) -> list[str]:
    """The ids of the listed messages whose line contains ``word``."""
    return [
        match.group(1) for line in text.splitlines()
        if word in line and (match := re.match(r"\[(m\d+)\]", line))
    ]  # fmt: skip


class Model:
    """A scripted model that plays all three parts, deciding by what it is shown."""

    def __init__(self, setup: Setup, start: Reply | None = None):
        self.setup = setup
        self.start = start or Call(
            "process_messages",
            {"folder": "INBOX", "matching": MATCHING, "instruction": INSTRUCTION},
        )
        self.screen: Callable[[str], str] = lambda listed: ", ".join(
            handles(listed, "ntern") + handles(listed, "Placement") + handles(listed, "Minutes 0")
        )
        self.seen: dict[str, list[Mapping[str, Any]]] = {"main": [], "screen": [], "one": []}
        setup.scripted.default = self

    def __call__(self, request: Mapping[str, Any]) -> Reply:
        kind = stage(request)
        self.seen[kind].append(request)
        messages = request["messages"]
        if kind == "main":
            return self.start if len(self.seen["main"]) == 1 else "All done."
        if kind == "screen":
            return self.screen(messages[1]["content"])
        return self.one(messages)

    def one(self, messages: list[dict[str, Any]]) -> Reply:
        shown = messages[1]["content"]
        handle = re.search(r"\[(m\d+)\]", shown).group(1)  # type: ignore[union-attr]
        if messages[-1]["role"] == "tool":
            return "Done as instructed."
        if "Cambridge" in shown:
            return Call("mark_messages", {"ids": [handle], "flagged": True})
        if "College" in shown:
            return Call("draft_reply", {"id": handle, "body": "I cannot take interns. Robin"})
        return "Skipped"


def total(events: list[Any]) -> str:
    """The line that sums up what process_messages did."""
    lines = [e.data["tool"]["summary"] for e in events if e.kind == "item" and e.data.get("tool")]
    return str(lines[-1])


def flagged_subjects(setup: Setup) -> list[str]:
    return sorted(m.subject for m in setup.store.search("", limit=50) if m.flagged)


def test_many_messages_are_worked_through_one_at_a_time(setup: Setup) -> None:
    model = Model(setup)
    events = setup.say(REQUEST)

    # What happened to the mail: one flag, two drafts, and nothing else.
    assert flagged_subjects(setup) == ["Summer internship"]
    drafts = setup.fake.folder("work", "Drafts").messages
    assert len(drafts) == 2
    assert all(b"I cannot take interns." in draft.raw for draft in drafts.values())
    assert setup.mail_changed == 1

    # The stages: the list was screened once (8 candidates fit in one screenful), and each of
    # the four messages picked was put before the model by itself.
    assert len(model.seen["screen"]) == 1
    assert "Wanted: requests for an internship" in model.seen["screen"][0]["messages"][1]["content"]
    assert "tools" not in model.seen["screen"][0]
    firsts = [r for r in model.seen["one"] if len(r["messages"]) == 2]
    assert len(firsts) == 4
    for request in firsts:
        system, shown = (m["content"] for m in request["messages"])
        assert MATCHING in system
        assert INSTRUCTION in system
        assert shown.count("<email") == 1, "one message, and nothing of the conversation"
        assert sorted(t["function"]["name"] for t in request["tools"]) == [
            "draft_reply",
            "mark_messages",
            "move_messages",
        ]

    # What the user sees: progress, one line per message that something was done to, a total.
    assert "progress" in kinds(events)
    lines = [e.data["tool"]["summary"] for e in events if e.kind == "item" and e.data.get("tool")]
    assert lines == [
        "Summer internship: Marked 1 message as flagged",
        "Internship request: Saved a draft reply to dana.whitlock@students.example in Drafts",
        "Placement: Saved a draft reply to tom.zielinski@students.example in Drafts",
        "Went through 8 messages: acted on 3, left 1 alone",
    ]
    assert events[-2].data["text"] == "All done."

    # What the model is told afterwards: a short report, not the messages themselves.
    report = model.seen["main"][1]["messages"][-1]
    assert report["role"] == "tool"
    assert "Looked at 8 messages." in report["content"]
    assert "4 of them could fit the description." in report["content"]
    assert "Acted on 3, left 1 alone because they did not fit." in report["content"]
    assert "Note: Done as instructed." in report["content"]
    everything = json.dumps(model.seen["main"][1]["messages"])
    assert "Northfield College" not in everything


def test_such_a_request_is_recognised_without_the_model_choosing_the_tool(setup: Setup) -> None:
    # The model under test never calls process_messages by itself: asked directly, it answers.
    model = Model(setup, "I would rather search.")
    setup.scripted.route = {"each": True, "which": MATCHING, "folder": "", "days": 0}
    events = setup.say(REQUEST)

    # The question that sorts the request is put with the request alone.
    (routing,) = setup.scripted.route_requests
    assert [m["role"] for m in routing["messages"]] == ["system", "user"]
    assert routing["messages"][1]["content"] == REQUEST
    assert "tools" not in routing

    # The work was done, and the model was only asked to report on it.
    assert flagged_subjects(setup) == ["Summer internship"]
    assert len(setup.fake.folder("work", "Drafts").messages) == 2
    assert total(events) == "Went through 8 messages: acted on 3, left 1 alone"
    (report,) = model.seen["main"]
    call = report["messages"][-2]["tool_calls"][0]["function"]
    assert call["name"] == "process_messages"
    assert json.loads(call["arguments"]) == {
        "matching": MATCHING,
        "instruction": REQUEST,
        "folder": "inbox",
    }
    # Each message is dealt with according to the user's own words.
    assert f"What is to be done: {REQUEST}" in model.seen["one"][0]["messages"][0]["content"]


def test_the_messages_are_not_gone_through_twice(setup: Setup) -> None:
    # After the work is done, this model asks for all of it again instead of reporting.
    model = Model(setup)
    setup.scripted.route = {"each": True, "which": MATCHING, "folder": "", "days": 0}
    events = setup.say(REQUEST)
    assert len(setup.fake.folder("work", "Drafts").messages) == 2, "no second set of drafts"
    again = [e.data["tool"] for e in events if e.kind == "item" and e.data.get("tool")][-1]
    assert again == {
        "name": "process_messages",
        "summary": "Asked to go through the messages a second time",
        "ok": False,
    }
    assert model.seen["main"][1]["messages"][-1]["content"] == agent_module.BATCH_ONCE
    assert events[-2].data["text"] == "All done."


def test_where_and_how_far_back_come_from_the_request(setup: Setup) -> None:
    setup.deliver("Archive", subject="Old internship", text="An internship, long ago.\n")
    setup.sync()
    model = Model(setup, "Done.")
    setup.scripted.route = {"each": True, "which": MATCHING, "folder": "all", "days": 30}
    events = setup.say(REQUEST)
    arguments = json.loads(
        model.seen["main"][0]["messages"][-2]["tool_calls"][0]["function"]["arguments"]
    )
    assert arguments == {"matching": MATCHING, "instruction": REQUEST, "days": 30}
    assert total(events).startswith("Went through 9 messages")


@pytest.mark.parametrize(
    "route",
    [
        {"each": False, "which": "", "folder": "", "days": 0},
        {"each": True, "which": "  ", "folder": "", "days": 0},
        {"each": "yes", "which": MATCHING, "folder": "", "days": 0},
        "this is not JSON",
        "[1, 2]",
    ],
)
def test_anything_else_is_left_to_the_model(setup: Setup, route: Any) -> None:
    setup.scripted.route = route
    events = setup.say("What is new?", "Nothing much.")
    assert kinds(events) == ["item", "delta", "delta", "item", "done"]
    assert len(setup.scripted.requests) == 1
    assert len(setup.scripted.route_requests) == 1


def test_mail_that_arrives_is_not_sorted_as_a_request(setup: Setup) -> None:
    setup.policy.save("Flag everything from Cambridge.\n", author="user")
    key = next(m.key for m in setup.store.search("Cambridge", limit=5))
    setup.scripted.default = "No rule applies."
    assert setup.agent.apply_policy(key) is False
    assert setup.scripted.route_requests == []


def test_a_message_cannot_make_the_model_act_on_another(setup: Setup) -> None:
    model = Model(setup)
    everything = [f"m{n}" for n in range(1, 9)]

    def greedy(messages: list[dict[str, Any]]) -> Reply:
        if messages[-1]["role"] == "tool":
            return "Tried."
        return Call("move_messages", {"ids": everything, "destination": "trash"})

    model.one = greedy  # type: ignore[method-assign]
    events = setup.say(REQUEST)
    assert len(setup.fake.folder("work", "INBOX").messages) == 8, "nothing was moved"
    failed = [
        e.data["tool"] for e in events
        if e.kind == "item" and e.data.get("tool") and not e.data["tool"]["ok"]
    ]  # fmt: skip
    assert len(failed) == 4
    assert all("only the message" in tool["summary"] for tool in failed)
    assert "failed on 4" in total(events)
    assert setup.mail_changed == 0


def test_messages_given_by_id_are_not_screened(setup: Setup) -> None:
    search = Call("search_mail", {"query": "internship"})
    setup.scripted.replies.append(search)
    model = Model(setup, Call("process_messages", {"ids": ["m1", "m2"], "instruction": "x"}))
    setup.say(REQUEST)
    assert model.seen["screen"] == []
    assert len([r for r in model.seen["one"] if len(r["messages"]) == 2]) == 2
    assert "every message you are shown" in model.seen["one"][0]["messages"][0]["content"]


def test_an_unusable_screening_answer_keeps_the_messages(setup: Setup) -> None:
    model = Model(setup)
    model.screen = lambda listed: "I am not sure what you mean."
    setup.say(REQUEST)
    assert len([r for r in model.seen["one"] if len(r["messages"]) == 2]) == 8
    assert flagged_subjects(setup) == ["Summer internship"]

    # "none" is an answer, though.
    other = Model(setup)
    other.screen = lambda listed: "None of them."
    events = setup.say(REQUEST)
    assert other.seen["one"] == []
    assert total(events) == "Went through 8 messages: acted on 0"


def test_what_does_not_fit_into_one_call_is_handed_back(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_module, "BATCH_MAX", 2)
    model = Model(setup)
    events = setup.say(REQUEST)
    assert len([r for r in model.seen["one"] if len(r["messages"]) == 2]) == 2
    report = model.seen["main"][1]["messages"][-1]["content"]
    assert "2 messages have not been looked at yet." in report
    remaining = re.search(r"these ids: (.*)$", report).group(1)  # type: ignore[union-attr]
    assert len(remaining.split(", ")) == 2
    assert "2 still to do" in total(events)


def test_the_user_can_stop_a_long_job(setup: Setup) -> None:
    model = Model(setup)
    one = model.one

    def stop_after_the_first(messages: list[dict[str, Any]]) -> Reply:
        reply = one(messages)
        if messages[-1]["role"] == "tool":
            setup.agent.stop()
        return reply

    model.one = stop_after_the_first  # type: ignore[method-assign]
    events = setup.say(REQUEST)
    assert flagged_subjects(setup) == ["Summer internship"]
    assert len(setup.fake.folder("work", "Drafts").messages) == 0
    assert events[-2].data["text"] == STOPPED
    assert len(model.seen["main"]) == 1, "the model is not asked to carry on"
    assert "3 still to do" in total(events)
    assert setup.mail_changed == 1

    # The next request starts afresh.
    assert not setup.agent.stopping or setup.say("Hello", "Hi")[-2].data["text"] == "Hi"
    assert setup.say("Hello again", "Hi again")[-2].data["text"] == "Hi again"


def test_an_answer_can_be_stopped_while_it_is_written(setup: Setup) -> None:
    replies = iter(setup.agent.reply(setup.conversation, "Tell me a long story"))
    setup.scripted.replies.append("Once upon a time there was a mailbox.")
    seen = []
    for event in replies:
        seen.append(event)
        if event.kind == "delta":
            setup.agent.stop()
    assert [e.kind for e in seen] == ["item", "delta", "item", "done"]
    assert seen[2].data["text"] == STOPPED


def test_out_of_steps_the_model_says_how_far_it_got(setup: Setup) -> None:
    def model(request: Mapping[str, Any]) -> Reply:
        if "tools" in request:
            return Call("search_mail", {"query": f"invoice {len(setup.scripted.requests)}"})
        return "So far I have found the invoice [m1]."

    setup.scripted.default = model
    events = setup.say("Loop forever")
    assert len(setup.scripted.requests) == agent_module.MAX_STEPS + 1
    last = setup.scripted.requests[-1]
    assert "tools" not in last
    assert last["messages"][-1] == {"role": "user", "content": WRAP_UP}
    assert events[-2].data["text"] == "So far I have found the invoice [m1]."
    # The note to the model is not part of the conversation.
    assert WRAP_UP not in json.dumps(setup.items())

    # If the model has nothing to say even then, the user is at least told what happened.
    setup.scripted.default = Call("list_folders")
    events = setup.say("Loop again")
    assert events[-2].data["text"] == OUT_OF_STEPS


def test_a_repeated_search_is_not_run_again(setup: Setup) -> None:
    search = Call("search_mail", {"query": "invoice"})
    events = setup.say("What do I owe?", search, search, "Invoice 1042, see [m1].")
    first, second = (e.data for e in events if e.kind == "item" and e.data.get("tool"))
    assert first["tool"]["ok"] is True
    assert second["tool"] == {
        "name": "search_mail",
        "summary": "Asked for the same thing again",
        "ok": False,
    }
    assert setup.scripted.last_messages()[-1]["content"] == REPEATED
    # In the next turn the same search is a new one.
    events = setup.say("And again?", search, "Still invoice 1042.")
    assert [e.data["tool"]["ok"] for e in events if e.kind == "item" and e.data.get("tool")] == [
        True
    ]


def test_a_search_says_when_there_is_more(setup: Setup) -> None:
    setup.say("Minutes?", Call("search_mail", {"query": "minutes", "limit": 3}), "Here.")
    content = setup.scripted.last_messages()[-1]["content"]
    assert content.startswith("3 messages found:")
    assert "More messages match than are shown here." in content
    assert "process_messages" in content
    tool = next(i["tool"] for i in setup.items() if i.get("tool"))
    assert tool["summary"] == "Searched for “minutes”: 3 messages (there are more)"

    setup.say("All?", Call("search_mail", {"query": "minutes", "limit": 5}), "Here.")
    assert "More messages match" not in setup.scripted.last_messages()[-1]["content"]


def test_old_tool_results_make_room() -> None:
    def tool(content: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": "c", "content": content}

    messages = [
        {"role": "system", "content": "s" * 100},
        {"role": "user", "content": "question"},
        tool("a" * 1000),
        tool("b" * 1000),
        tool("c" * 1000),
    ]
    assert fit_context(messages, 10_000) is messages
    fitted = fit_context(messages, 2500)
    assert [len(m["content"]) for m in fitted] == [100, 8, 48, 1000, 1000]
    assert fitted[2]["content"].startswith("(This result is no longer shown")
    assert len(messages[2]["content"]) == 1000, "the original is left alone"
    # If even that is not enough, everything that can go, goes.
    assert [len(m["content"]) for m in fit_context(messages, 10)] == [100, 8, 48, 48, 48]

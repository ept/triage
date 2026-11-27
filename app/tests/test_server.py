"""The app's HTTP API, with the fake gateway and a scripted model behind it."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from triage.server import App
from triage.testing.fake_gateway import FakeGateway
from triage.testing.fake_llm import Call, ScriptedLlm
from triage.testing.samples import ALEX, KIM, make_message

NOW = datetime.now(UTC)


class Setup:
    def __init__(self, tmp_path: Path, fake: FakeGateway):
        self.fake = fake
        self.scripted = ScriptedLlm()
        fake.llm_handler = self.scripted.handler
        web = tmp_path / "web"
        web.mkdir()
        (web / "index.html").write_text("<!doctype html><title>Email Triage</title>")
        (web / "app.js").write_text("console.log('hi')")
        self.app = App(
            gateway_url="http://gateway.test",
            cache_dir=tmp_path / "cache",
            config_dir=tmp_path / "config",
            transport=fake.transport(),
            web_dir=web,
        )
        self.http = TestClient(self.app.asgi)

    def deliver(self, **kwargs: Any) -> int:
        kwargs.setdefault("date", NOW - timedelta(hours=5))
        return self.fake.add_message(
            "work", "INBOX", make_message(**kwargs), internal_date=kwargs["date"]
        )

    def chat(self, conversation: int, text: str) -> list[tuple[str, dict[str, Any]]]:
        response = self.http.post(
            f"/api/conversations/{conversation}/messages", json={"text": text}
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        events = []
        for block in response.text.strip().split("\n\n"):
            lines = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append((lines["event"], json.loads(lines["data"])))
        return events


@pytest.fixture
def setup(tmp_path: Path, fake: FakeGateway) -> Iterator[Setup]:
    env = Setup(tmp_path, fake)
    env.deliver(sender=ALEX, subject="Invoice 1042", text="Please pay invoice 1042.\n")
    env.deliver(
        sender=KIM,
        subject="Newsletter",
        text="Plain version\n\nOn Monday, Robin wrote:\n> earlier text\n",
        html='<p>Hello <b>Robin</b></p><img src="https://tracker.example/p.gif"><script>alert(1)</script>',
        attachments=[("report.pdf", "application/pdf", b"%PDF-")],
    )
    assert env.app.sync.sync_all().errors == []
    yield env
    env.app.close()


def test_health_status_and_static_files(setup: Setup) -> None:
    assert setup.http.get("/healthz").json() == {"status": "ok"}
    status = setup.http.get("/api/status").json()
    assert status["accounts"] == [
        {"id": "work", "address": "robin@example.org", "display_name": "Robin Tester"}
    ]
    assert status["llm"] == {"available": True, "model": "scripted-test-model", "error": None}
    assert status["sync"]["state"] == "idle"
    assert status["sync"]["messages_done"] == 2
    assert {"account": "work", "folder": "INBOX", "total": 2, "unread": 2} in status["counts"]
    assert status["pending_change_requests"] == 0
    assert status["notices"] == []

    page = setup.http.get("/")
    assert page.status_code == 200
    assert "Email Triage" in page.text
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "no-cache"
    assert setup.http.get("/app.js").status_code == 200
    assert setup.http.post("/api/sync").status_code == 202

    missing = setup.http.get("/api/nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_llm_unavailable_is_reported(setup: Setup) -> None:
    setup.fake.llm_handler = None
    status = setup.http.get("/api/status").json()
    assert status["llm"]["available"] is False
    assert status["llm"]["error"]


def test_chat_streams_events_and_stores_the_conversation(setup: Setup) -> None:
    conversation = setup.http.post("/api/conversations").json()["id"]
    setup.scripted.replies += [Call("search_mail", {"query": "invoice"}), "See [m1]."]
    events = setup.chat(conversation, "What should I pay?")
    assert [kind for kind, _ in events] == ["item", "item", "delta", "delta", "item", "done"]
    assert events[1][1]["tool"]["name"] == "search_mail"
    card = events[1][1]["messages"][0]
    assert card["handle"] == "m1"
    assert card["subject"] == "Invoice 1042"
    assert events[4][1]["text"] == "See [m1]."

    stored = setup.http.get(f"/api/conversations/{conversation}").json()
    assert stored["title"] == "What should I pay?"
    assert [item["role"] for item in stored["items"]] == ["user", "tool", "assistant"]
    assert all("llm" not in item for item in stored["items"])
    listed = setup.http.get("/api/conversations").json()["conversations"]
    assert [c["id"] for c in listed] == [conversation]

    # The card's key opens the message.
    message = setup.http.get(f"/api/messages/{card['key']}").json()
    assert message["subject"] == "Invoice 1042"
    assert message["from"] == [{"name": "Alex Sample", "address": "alex@example.com"}]
    assert message["text"]["body"].strip() == "Please pay invoice 1042."
    assert message["has_html"] is False

    for bad in [
        ("/api/conversations/999", "get", None),
        ("/api/conversations/999/messages", "post", {"text": "x"}),
        (f"/api/conversations/{conversation}/messages", "post", {"text": "  "}),
        (f"/api/conversations/{conversation}/messages", "post", {"text": "x" * 20_001}),
        (f"/api/conversations/{conversation}/messages", "post", ["not", "an", "object"]),
    ]:
        path, method, body = bad
        response = setup.http.request(method, path, json=body)
        assert response.status_code in (400, 404), path
        assert "error" in response.json()


def test_only_one_question_at_a_time(setup: Setup) -> None:
    conversation = setup.http.post("/api/conversations").json()["id"]
    assert setup.app._agent_lock.acquire(blocking=False)
    try:
        events = setup.chat(conversation, "Hello?")
    finally:
        setup.app._agent_lock.release()
    assert [kind for kind, _ in events] == ["error", "done"]
    assert "busy" in events[0][1]["message"]
    assert setup.http.get(f"/api/conversations/{conversation}").json()["items"] == []


def test_html_messages_are_served_for_a_sandboxed_frame(setup: Setup) -> None:
    found = setup.http.get("/api/search", params={"q": "newsletter"}).json()["messages"]
    assert len(found) == 1
    key = found[0]["key"]
    message = setup.http.get(f"/api/messages/{key}").json()
    assert message["has_html"] is True
    assert message["blocked_remote_content"] == 1
    assert message["attachments"] == [
        {"filename": "report.pdf", "content_type": "application/pdf", "size": 5}
    ]
    assert message["text"]["body"].strip() == "Plain version"
    assert "earlier text" in message["text"]["quoted"]

    html = setup.http.get(f"/api/messages/{key}/html")
    assert html.status_code == 200
    policy = html.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "sandbox" in policy
    assert "<b>Robin</b>" in html.text
    assert "tracker.example" not in html.text
    assert "alert(1)" not in html.text
    assert "<script" not in html.text.lower()

    plain = setup.http.get("/api/search", params={"q": "invoice"}).json()["messages"][0]["key"]
    assert setup.http.get(f"/api/messages/{plain}/html").status_code == 404
    assert setup.http.get("/api/messages/garbage").status_code == 404
    assert setup.http.get("/api/messages/garbage/html").status_code == 404
    assert setup.http.get("/api/search", params={"limit": "x"}).status_code == 400
    unread = setup.http.get("/api/search", params={"unread": "1", "limit": "1"}).json()["messages"]
    assert len(unread) == 1


def test_activity_and_undo(setup: Setup) -> None:
    conversation = setup.http.post("/api/conversations").json()["id"]
    setup.scripted.replies += [
        Call("search_mail", {"query": "invoice"}),
        Call("move_messages", {"ids": ["m1"], "destination": "trash"}),
        "Moved.",
    ]
    setup.chat(conversation, "Delete the invoice")
    trash = setup.fake.accounts["work"].folders["Trash"].messages
    assert len(trash) == 1

    entries = setup.http.get("/api/activity").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["summary"] == "Moved 1 message from INBOX to Trash"
    assert entries[0]["undoable"] is True
    assert entries[0]["undone"] is False

    assert setup.http.post(f"/api/activity/{entries[0]['seq']}/undo").status_code == 200
    assert len(trash) == 0
    assert len(setup.fake.accounts["work"].folders["INBOX"].messages) == 2
    after = setup.http.get("/api/activity").json()
    assert [e["undone"] for e in after["entries"]] == [True, False]
    assert after["last_seq"] == 2
    assert setup.http.post(f"/api/activity/{entries[0]['seq']}/undo").status_code == 400
    assert setup.http.post("/api/activity/99/undo").status_code == 404
    assert setup.http.get("/api/activity", params={"after": "x"}).status_code == 400


def test_policy_editing_history_and_proposals(setup: Setup) -> None:
    assert setup.http.get("/api/policy").json() == {"text": "", "updated": None, "proposals": []}
    assert setup.http.put("/api/policy", json={"text": "Archive newsletters."}).status_code == 200
    assert setup.http.put("/api/policy", json={"text": 5}).status_code == 400

    conversation = setup.http.post("/api/conversations").json()["id"]
    setup.scripted.replies += [
        Call("propose_policy_change", {"new_policy": "Archive newsletters.\nFlag invoices."}),
        "I proposed it.",
    ]
    events = setup.chat(conversation, "From now on flag invoices")
    proposal = events[1][1]["policy_proposal"]
    assert proposal["state"] == "pending"
    assert setup.http.get("/api/status").json()["pending_policy_proposals"] == 1

    policy = setup.http.get("/api/policy").json()
    assert policy["text"] == "Archive newsletters.\n"
    assert policy["proposals"][0]["id"] == proposal["id"]
    assert (
        setup.http.post(
            f"/api/policy/proposals/{proposal['id']}", json={"accept": "yes"}
        ).status_code
        == 400
    )
    assert (
        setup.http.post(
            f"/api/policy/proposals/{proposal['id']}", json={"accept": True}
        ).status_code
        == 200
    )
    assert (
        setup.http.post(
            f"/api/policy/proposals/{proposal['id']}", json={"accept": True}
        ).status_code
        == 400
    )
    assert setup.http.get("/api/policy").json()["text"] == "Archive newsletters.\nFlag invoices.\n"

    # The conversation shows the proposal with its current state.
    items = setup.http.get(f"/api/conversations/{conversation}").json()["items"]
    assert items[1]["policy_proposal"]["state"] == "accepted"

    versions = setup.http.get("/api/policy/history").json()["versions"]
    assert [v["summary"] for v in versions] == ["Suggested by the assistant", "Edited by the user"]
    old = setup.http.get(f"/api/policy/history/{versions[1]['id']}").json()
    assert old == {"text": "Archive newsletters.\n"}
    assert setup.http.get("/api/policy/history/zzz").status_code == 404


def test_change_requests_and_the_coding_agent(setup: Setup) -> None:
    conversation = setup.http.post("/api/conversations").json()["id"]
    setup.scripted.replies += [Call("request_app_change")]
    events = setup.chat(conversation, "Please add a dark mode")
    request = events[1][1]["change_request"]
    assert request["state"] == "pending"
    assert request["text"] == "Please add a dark mode"
    assert setup.http.get("/api/status").json()["pending_change_requests"] == 1

    # A message typed for the coding agent directly goes the same way.
    direct = setup.http.post("/api/coding/requests", json={"kind": "reply", "text": "Yes, blue."})
    assert direct.status_code == 202
    assert (
        setup.http.post("/api/coding/requests", json={"kind": "bug", "text": "x"}).status_code
        == 400
    )
    hidden = setup.http.post("/api/coding/requests", json={"kind": "feature", "text": "a​b"})
    assert hidden.status_code == 403
    assert hidden.json()["error"]["code"] == "forbidden"

    coding = setup.http.get("/api/coding").json()
    assert [(r["kind"], r["state"], r["text"]) for r in coding["requests"]] == [
        ("feature", "pending", "Please add a dark mode"),
        ("reply", "pending", "Yes, blue."),
    ]
    assert coding["events"] == []

    # The user approves one in the wrapper app; the coding agent answers.
    setup.fake.decide_change_request(request["id"], approve=True)
    setup.fake.push_coding_event("status", "Edit: app.css", request["id"])
    setup.fake.push_coding_event("question", "<b>Which colour?</b>", request["id"])
    coding = setup.http.get("/api/coding", params={"after": "0", "wait": "0"}).json()
    assert [r["state"] for r in coding["requests"]] == ["approved", "pending"]
    assert [(e["kind"], e["text"]) for e in coding["events"]] == [
        ("status", "Edit: app.css"),
        ("question", "<b>Which colour?</b>"),
    ]
    assert coding["events"][0]["request_id"] == request["id"]
    assert coding["last_seq"] == 2
    assert setup.http.get("/api/coding", params={"after": "2"}).json()["events"] == []
    items = setup.http.get(f"/api/conversations/{conversation}").json()["items"]
    assert items[1]["change_request"]["state"] == "approved"
    assert setup.http.get("/api/coding", params={"after": "x"}).status_code == 400


def test_notifications_from_the_gateway(setup: Setup) -> None:
    when = datetime(2026, 11, 20, tzinfo=UTC)
    setup.app._handle_event(
        "deletion_rate", when, {"account": "work", "count": 23, "window_minutes": 60}
    )
    notices = setup.http.get("/api/status").json()["notices"]
    assert len(notices) == 1
    assert "23 messages were moved to the trash" in notices[0]["text"]


def test_the_assistant_can_be_stopped(setup: Setup) -> None:
    # Nothing is running: the request is accepted and has no effect on the next question.
    assert setup.http.post("/api/assistant/stop", json={}).status_code == 200
    assert not setup.app.agent.stopping
    conversation = setup.http.post("/api/conversations", json={}).json()["id"]
    setup.scripted.replies.append("Hello.")
    assert setup.chat(conversation, "Hi")[-2][1]["text"] == "Hello."

    # While it is answering, the request ends the answer.
    def stop_and_answer(request: Any) -> str:
        assert setup.http.post("/api/assistant/stop", json={}).status_code == 200
        return "This answer is not wanted any more."

    setup.scripted.replies.append(stop_and_answer)
    events = setup.chat(conversation, "Tell me everything")
    assert events[-2][1]["text"].startswith("Stopped, as you asked.")


def test_account_changes_reach_the_running_app(tmp_path: Path, fake: FakeGateway) -> None:
    """Adding or removing an account needs no restart: the gateway announces the change."""
    env = Setup(tmp_path, fake)
    env.deliver(sender=ALEX, subject="Invoice 1042", text="Please pay invoice 1042.\n")

    def wait_for(condition: Callable[[], bool], what: str) -> None:
        pause = threading.Event()
        for _ in range(100):
            if condition():
                return
            pause.wait(0.1)
        pytest.fail(what)

    def status_accounts() -> list[str]:
        return [a["id"] for a in env.http.get("/api/status").json()["accounts"]]

    env.app.start()
    try:
        wait_for(lambda: bool(env.app.store.search("invoice")), "the first sync did not happen")
        assert status_accounts() == ["work"]

        fake.add_account("home", "robin@home.example", "Robin Tester")
        fake.add_message(
            "home",
            "INBOX",
            make_message(sender=KIM, subject="Allotment rota", text="Your turn on Saturday.\n"),
            internal_date=NOW - timedelta(hours=2),
        )
        wait_for(
            lambda: bool(env.app.store.search("allotment")),
            "mail of the added account was not synced",
        )
        assert status_accounts() == ["work", "home"]

        fake.remove_account("work")
        wait_for(
            lambda: not env.app.store.search("invoice"),
            "mail of the removed account stayed in the cache",
        )
        assert status_accounts() == ["home"]
        assert env.app.store.search("allotment")
    finally:
        env.app.close()


def test_background_threads_sync_and_analyse(tmp_path: Path, fake: FakeGateway) -> None:
    env = Setup(tmp_path, fake)
    env.scripted.default = (
        '{"summary": "A message.", "category": "work", "needs_reply": true, '
        '"urgency": "normal", "deadline": null}'
    )
    env.deliver(sender=ALEX, subject="Question", text="Can you send the report?\n")
    env.app.start()
    try:
        deadline = threading.Event()
        for _ in range(100):
            found = env.app.store.search("report")
            if found and env.app.store.get_analysis(found[0].key):
                break
            deadline.wait(0.1)
        else:
            pytest.fail("the message was not synced and analysed in the background")
        analysis = env.app.store.get_analysis(found[0].key)
        assert analysis is not None
        assert analysis.result["needs_reply"] is True
    finally:
        env.app.close()


def test_settings(setup: Setup) -> None:
    assert setup.http.get("/api/settings").json() == {"apply_policy_automatically": False}
    changed = setup.http.put("/api/settings", json={"apply_policy_automatically": True})
    assert changed.json() == {"apply_policy_automatically": True}
    assert setup.http.get("/api/settings").json() == {"apply_policy_automatically": True}
    for bad in [{"apply_policy_automatically": "yes"}, {"unknown": True}]:
        assert setup.http.put("/api/settings", json=bad).status_code == 400
    assert setup.http.get("/api/settings").json() == {"apply_policy_automatically": True}


def test_policy_is_applied_automatically_only_when_switched_on(setup: Setup) -> None:
    setup.http.put("/api/policy", json={"text": "Move invoices to the archive."})
    invoice = setup.app.store.search("invoice")[0]
    setup.scripted.default = "No rule applies."

    # Off by default: the model is not asked.
    assert setup.app.apply_policy_to(invoice) is False
    assert setup.scripted.requests == []

    setup.http.put("/api/settings", json={"apply_policy_automatically": True})
    setup.scripted.replies += [
        Call("move_messages", {"ids": ["m1"], "destination": "archive"}),
        "Moved, as the rule about invoices says.",
    ]
    assert setup.app.apply_policy_to(invoice) is True
    assert len(setup.fake.accounts["work"].folders["Archive"].messages) == 1
    listed = setup.http.get("/api/conversations").json()["conversations"]
    assert [c["title"] for c in listed] == ["Automatic actions"]
    activity_entries = setup.http.get("/api/activity").json()["entries"]
    assert activity_entries[0]["summary"] == "Moved 1 message from INBOX to Archive"

    # Mail that is old, already read, or outside the inbox is left alone.
    asked = len(setup.scripted.requests)
    old = setup.deliver(sender=ALEX, subject="Old invoice", text="x", date=NOW - timedelta(days=9))
    del old
    setup.app.sync.sync_all()
    stale = setup.app.store.search("old invoice")[0]
    assert setup.app.apply_policy_to(stale) is False
    archived = setup.app.store.search("invoice 1042")[0]
    assert archived.key.folder == "Archive"
    assert setup.app.apply_policy_to(archived) is False
    assert len(setup.scripted.requests) == asked

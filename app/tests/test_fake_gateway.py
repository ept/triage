"""The fake gateway must enforce the rules of the real one, or tests against it prove little."""

from __future__ import annotations

import email
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from triage.testing.fake_gateway import FakeGateway
from triage.testing.samples import ALEX, KIM, USER, demo_gateway, make_message


def call(fake: FakeGateway, method: str, path: str, body: dict[str, Any] | None = None):
    response = fake.handle(method, path, json.dumps(body).encode() if body is not None else b"")
    assert isinstance(response.body, bytes)
    return response.status, json.loads(response.body)


def post(fake: FakeGateway, path: str, body: dict[str, Any]):
    return call(fake, "POST", path, body)


def error_code(result) -> str:
    return result[1]["error"]["code"]


def inbox(fake: FakeGateway) -> dict[str, Any]:
    return {
        "account": "work",
        "folder": "INBOX",
        "uid_validity": fake.folder("work", "INBOX").uid_validity,
    }


def test_unknown_endpoints_methods_and_bodies(fake: FakeGateway) -> None:
    assert call(fake, "GET", "/v1/nothing")[0] == 404
    assert call(fake, "GET", "/v1/folders/list")[0] == 400
    assert call(fake, "POST", "/v1/accounts", {})[0] == 400
    assert fake.handle("POST", "/v1/folders/list", b"not json").status == 400
    assert fake.handle("POST", "/v1/folders/list", b"[]").status == 400
    assert post(fake, "/v1/folders/list", {})[0] == 400
    assert post(fake, "/v1/folders/list", {"account": 7})[0] == 400
    assert error_code(post(fake, "/v1/folders/list", {"account": "nobody"})) == "not_found"


def test_folder_rules(fake: FakeGateway) -> None:
    fake.add_folder("work", "Shared", selectable=False)
    assert post(fake, "/v1/folders/status", {"account": "work", "folder": "Shared"})[0] == 400
    assert post(fake, "/v1/folders/status", {"account": "work", "folder": "Nope"})[0] == 404

    for name in ("Drafts", "drafts", "Outbox", "Work/Sent Items", "Templates"):
        result = post(fake, "/v1/folders/create", {"account": "work", "name": name})
        assert error_code(result) == "forbidden", name
    for name in ("", "bad\x00name", "x" * 201):
        result = post(fake, "/v1/folders/create", {"account": "work", "name": name})
        assert error_code(result) == "bad_request", repr(name)
    assert error_code(post(fake, "/v1/folders/create", {"account": "work", "name": "Archive"})) == (
        "imap_error"
    )
    assert post(fake, "/v1/folders/create", {"account": "work", "name": "Triage"}) == (200, {})
    assert fake.log_entries[-1]["op"] == "create_folder"


def test_list_limits(fake: FakeGateway) -> None:
    base = {"account": "work", "folder": "INBOX"}
    assert post(fake, "/v1/messages/list", {**base, "limit": 2001})[0] == 400
    assert post(fake, "/v1/messages/list", {**base, "limit": 0})[0] == 400
    assert post(fake, "/v1/messages/list", {**base, "uid_min": 0})[0] == 400
    status, body = post(fake, "/v1/messages/list", {**base, "uid_max": None})
    assert status == 200
    assert body["messages"] == []
    assert body["next_uid"] is None


def test_fetch_limits(fake: FakeGateway) -> None:
    base = {"account": "work", "folder": "INBOX"}
    assert post(fake, "/v1/messages/fetch", {**base, "uids": []})[0] == 400
    assert post(fake, "/v1/messages/fetch", {**base, "uids": list(range(1, 102))})[0] == 400
    assert post(fake, "/v1/messages/fetch", {**base, "uids": [0]})[0] == 400
    assert post(fake, "/v1/messages/fetch", {**base, "uids": ["1"]})[0] == 400
    assert post(fake, "/v1/messages/fetch", {**base, "uids": [True]})[0] == 400


@pytest.mark.parametrize(
    ("flag", "code"),
    [
        ("\\Deleted", "forbidden"),
        ("\\Draft", "forbidden"),
        ("\\Recent", "forbidden"),
        ("\\seen", "forbidden"),
        ("has space", "bad_request"),
        ("1starts-with-digit", "bad_request"),
        ("", "bad_request"),
        ("x" * 65, "bad_request"),
        ("paren(", "bad_request"),
    ],
)
def test_forbidden_and_malformed_flags(fake: FakeGateway, flag: str, code: str) -> None:
    fake.add_message("work", "INBOX", make_message())
    for field in ("add", "remove"):
        result = post(fake, "/v1/messages/flags", {**inbox(fake), "uids": [1], field: [flag]})
        assert error_code(result) == code
    assert fake.folder("work", "INBOX").messages[1].flags == set()
    assert fake.log_entries == []


def test_flag_requests_need_validity_uids_and_changes(fake: FakeGateway) -> None:
    fake.add_message("work", "INBOX", make_message())
    request = {**inbox(fake), "uids": [1], "add": ["\\Seen"]}
    assert post(fake, "/v1/messages/flags", {**request, "uids": []})[0] == 400
    assert post(fake, "/v1/messages/flags", {**request, "uids": list(range(1, 502))})[0] == 400
    assert post(fake, "/v1/messages/flags", {**request, "add": []})[0] == 400
    missing = {k: v for k, v in request.items() if k != "uid_validity"}
    assert post(fake, "/v1/messages/flags", missing)[0] == 400
    assert post(fake, "/v1/messages/flags", {**request, "uid_validity": 1})[0] == 409
    assert post(fake, "/v1/messages/flags", {**request, "add": ["$Label_1.a-b"]})[0] == 200


@pytest.mark.parametrize("destination", ["Drafts", "Sent", "Outbox", "Templates"])
def test_cannot_move_into_protected_folders(fake: FakeGateway, destination: str) -> None:
    fake.add_folder("work", "Outbox")
    fake.add_folder("work", "Templates")
    fake.add_message("work", "INBOX", make_message())
    result = post(
        fake, "/v1/messages/move", {**inbox(fake), "uids": [1], "destination": destination}
    )
    assert error_code(result) == "forbidden"
    assert 1 in fake.folder("work", "INBOX").messages
    assert fake.log_entries == []


def test_move_rules(fake: FakeGateway) -> None:
    fake.add_folder("work", "Container", selectable=False)
    raw = make_message()
    fake.add_message("work", "INBOX", raw, flags={"\\Seen"})
    request = {**inbox(fake), "uids": [1], "destination": "Archive"}
    assert post(fake, "/v1/messages/move", {**request, "destination": "Nowhere"})[0] == 404
    assert post(fake, "/v1/messages/move", {**request, "destination": "INBOX"})[0] == 400
    assert post(fake, "/v1/messages/move", {**request, "destination": "Container"})[0] == 400
    assert post(fake, "/v1/messages/move", {**request, "uid_validity": 5})[0] == 409

    status, body = post(fake, "/v1/messages/move", {**request, "uids": [1, 1, 7]})
    assert status == 200
    assert body["moved"] == [{"uid": 1, "new_uid": 1}]
    moved = fake.folder("work", "Archive").messages[1]
    assert (moved.raw, moved.flags) == (raw, {"\\Seen"})
    assert fake.log_entries[-1]["moved"] == [{"uid": 1, "new_uid": 1}]

    # Moving out of a protected folder is allowed; only moving into one is not.
    fake.add_message("work", "Drafts", raw)
    out_of_drafts = {
        "account": "work",
        "folder": "Drafts",
        "uid_validity": fake.folder("work", "Drafts").uid_validity,
        "uids": [1],
        "destination": "Trash",
    }
    assert post(fake, "/v1/messages/move", out_of_drafts)[0] == 200


def test_deletion_rate_warning(fake: FakeGateway) -> None:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    fake._clock = lambda: now
    for _ in range(45):
        fake.add_message("work", "INBOX", make_message())

    def trash(uids: list[int], destination: str = "Trash") -> None:
        request = {**inbox(fake), "uids": uids, "destination": destination}
        assert post(fake, "/v1/messages/move", request)[0] == 200

    def warnings() -> list[dict[str, Any]]:
        return [e for e in fake.event_list if e["kind"] == "deletion_rate"]

    trash(list(range(1, 21)))
    assert warnings() == []  # exactly 20 is not yet "more than 20"
    trash([21], "Archive")
    assert warnings() == []  # archiving is not deleting
    trash([22], "Junk")
    assert len(warnings()) == 1
    assert warnings()[0]["account"] == "work"
    assert warnings()[0]["count"] == 21
    assert warnings()[0]["window_minutes"] == 60
    trash([23, 24])
    assert len(warnings()) == 1  # one warning per window, not one per message

    now += timedelta(minutes=61)
    trash([25])
    assert len(warnings()) == 1  # the old deletions no longer count
    trash(list(range(26, 46)))
    assert len(warnings()) == 2


def draft_request(**overrides: Any) -> dict[str, Any]:
    request = {
        "account": "work",
        "to": [{"name": ALEX[0], "address": ALEX[1]}],
        "cc": [],
        "subject": "Re: Hello",
        "body": "Hi Alex,\n\nthanks!\n",
    }
    request.update(overrides)
    return request


def test_new_draft(fake: FakeGateway) -> None:
    status, body = post(fake, "/v1/drafts", draft_request(subject="Grüße", body="Schöne Grüße\n"))
    assert status == 200
    assert body["folder"] == "Drafts"
    stored = fake.folder("work", "Drafts").messages[1]
    assert stored.flags == {"\\Draft", "\\Seen"}
    draft = email.message_from_bytes(stored.raw, policy=email.policy.default)
    assert draft["From"].addresses[0].addr_spec == USER[1]
    assert draft["To"].addresses[0].addr_spec == ALEX[1]
    assert str(draft["Subject"]) == "Grüße"
    assert draft["Message-ID"] == body["message_id"]
    assert draft["Bcc"] is None
    assert draft["Reply-To"] is None
    assert draft["In-Reply-To"] is None
    assert draft.get_content_type() == "text/plain"
    assert draft.get_content().replace("\r\n", "\n") == "Schöne Grüße\n"
    assert fake.log_entries[-1] == {
        "seq": 1,
        "time": fake.log_entries[-1]["time"],
        "account": "work",
        "op": "draft",
        "folder": "Drafts",
        "message_id": body["message_id"],
    }


def test_reply_draft_recipient_rule(fake: FakeGateway) -> None:
    original = make_message(
        sender=ALEX,
        to=[USER],
        cc=[KIM],
        message_id="<m2@example.com>",
        references=["<m1@example.com>"],
        headers=[("Reply-To", "replies@example.com")],
    )
    fake.add_message("work", "INBOX", original)
    reference = {k: v for k, v in inbox(fake).items() if k != "account"} | {"uid": 1}

    # Any subset of the participants is fine, compared case-insensitively.
    ok = draft_request(
        to=[{"address": "REPLIES@example.com"}],
        cc=[{"address": KIM[1], "name": None}, {"address": ALEX[1]}],
        in_reply_to=reference,
    )
    status, _body = post(fake, "/v1/drafts", ok)
    assert status == 200
    draft = email.message_from_bytes(fake.folder("work", "Drafts").messages[1].raw)
    assert draft["In-Reply-To"] == "<m2@example.com>"
    assert draft["References"] == "<m1@example.com> <m2@example.com>"
    assert fake.log_entries[-1]["in_reply_to"] == reference

    # Nobody else may be added, neither in To nor in Cc.
    for field in ("to", "cc"):
        bad = draft_request(in_reply_to=reference)
        bad[field] = [*bad[field], {"address": "mallory@elsewhere.example"}]
        assert error_code(post(fake, "/v1/drafts", bad)) == "forbidden"
    assert len(fake.folder("work", "Drafts").messages) == 1

    stale = draft_request(in_reply_to={**reference, "uid_validity": 3})
    assert post(fake, "/v1/drafts", stale)[0] == 409
    gone = draft_request(in_reply_to={**reference, "uid": 99})
    assert post(fake, "/v1/drafts", gone)[0] == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"to": []},
        {"to": [{"address": "not an address"}]},
        {"to": [{"address": "a@example.org\r\nBcc: mallory@elsewhere.example"}]},
        {"to": [{"address": "a@example.org", "name": "Line\nBreak"}]},
        {"to": [{"address": f"user{i}@example.org"} for i in range(51)]},
        {"subject": "x" * 501},
        {"subject": "Hello\r\nBcc: mallory@elsewhere.example"},
        {"body": "x" * 100_001},
        {"bcc": [{"address": "mallory@elsewhere.example"}]},
        {"reply_to": "mallory@elsewhere.example"},
        {"headers": {"X-Anything": "1"}},
        {"in_reply_to": "INBOX/1"},
    ],
)
def test_draft_validation(fake: FakeGateway, overrides: dict[str, Any]) -> None:
    assert error_code(post(fake, "/v1/drafts", draft_request(**overrides))) == "bad_request"
    assert fake.folder("work", "Drafts").messages == {}


def test_draft_needs_a_drafts_folder(fake: FakeGateway) -> None:
    fake.remove_folder("work", "Drafts")
    assert error_code(post(fake, "/v1/drafts", draft_request())) == "unsupported"


def test_search_validation(fake: FakeGateway) -> None:
    def search(query: Any):
        return post(
            fake, "/v1/messages/search", {"account": "work", "folder": "INBOX", "query": query}
        )

    nested: dict[str, Any] = {"all": True}
    for _ in range(4):
        nested = {"not": nested}
    assert search(nested)[0] == 200  # five levels
    assert error_code(search({"not": nested})) == "bad_request"

    assert error_code(search({"and": []})) == "bad_request"
    assert error_code(search({"or": [{"all": True}] * 21})) == "bad_request"
    assert error_code(search({"subject": "x" * 201})) == "bad_request"
    assert error_code(search({"subject": "tab\there"})) == "bad_request"
    assert error_code(search({"subject": "naïve"})) == "unsupported"
    assert error_code(search({"since": "last week"})) == "bad_request"
    assert error_code(search({"seen": "yes"})) == "bad_request"
    assert error_code(search({"larger": -1})) == "bad_request"
    assert error_code(search({"uid": [1]})) == "bad_request"
    assert error_code(search({"header": ["Bad Name", "x"]})) == "bad_request"
    assert error_code(search({"raw": "ALL"})) == "bad_request"
    assert error_code(search({"all": True, "seen": True})) == "bad_request"
    assert error_code(search("ALL")) == "bad_request"


def test_search_semantics(fake: FakeGateway) -> None:
    day = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    fake.add_message(
        "work",
        "INBOX",
        make_message(sender=ALEX, subject="Quarterly budget", text="Numbers attached.\n", date=day),
        flags={"\\Seen", "$Work"},
        internal_date=day,
    )
    fake.add_message(
        "work",
        "INBOX",
        make_message(
            sender=KIM,
            cc=[ALEX],
            subject="Lunch?",
            text="Shall we try the new place?\n" * 50,
            date=day + timedelta(days=9),
            headers=[("List-Id", "<lunch.example.net>")],
        ),
        flags={"\\Flagged"},
        internal_date=day + timedelta(days=10),
    )

    def uids(query: Any) -> list[int]:
        status, body = post(
            fake, "/v1/messages/search", {"account": "work", "folder": "INBOX", "query": query}
        )
        assert status == 200, body
        return body["uids"]

    assert uids({"all": True}) == [1, 2]
    assert uids({"since": "2026-10-11"}) == [2]
    assert uids({"before": "2026-10-11"}) == [1]
    assert uids({"sent_since": "2026-10-11"}) == []
    assert uids({"sent_before": "2026-10-11"}) == [1, 2]
    assert uids({"from": "ALEX"}) == [1]
    assert uids({"cc": "alex"}) == [2]
    assert uids({"to": "robin"}) == [1, 2]
    assert uids({"subject": "budget"}) == [1]
    assert uids({"body": "new place"}) == [2]
    assert uids({"body": "budget"}) == []
    assert uids({"text": "budget"}) == [1]
    assert uids({"header": ["List-Id", "lunch"]}) == [2]
    assert uids({"seen": True}) == [1]
    assert uids({"seen": False}) == [2]
    assert uids({"flagged": True}) == [2]
    assert uids({"answered": False}) == [1, 2]
    assert uids({"keyword": "$Work"}) == [1]
    assert uids({"larger": 1000}) == [2]
    assert uids({"smaller": 1000}) == [1]
    assert uids({"uid": [2, 9]}) == [2]
    assert uids({"and": [{"from": "kim"}, {"flagged": True}]}) == [2]
    assert uids({"or": [{"from": "kim"}, {"keyword": "$Work"}]}) == [1, 2]
    assert uids({"not": {"from": "kim"}}) == [1]


def test_change_request_rules(fake: FakeGateway) -> None:
    def submit(kind: str, text: str):
        return call(fake, "POST", "/v1/change-requests", {"kind": kind, "text": text})

    assert submit("wish", "x")[0] == 400
    assert submit("feature", "")[0] == 400
    assert submit("feature", "   ")[0] == 400
    assert submit("feature", "bell\x07")[0] == 400
    assert submit("feature", "escape\x1b[31m")[0] == 400
    assert submit("bug", "line one\n\tline two")[0] == 202

    for _ in range(4):
        assert submit("feature", "another")[0] == 202
    assert error_code(submit("feature", "one too many")) == "forbidden"
    first = next(iter(fake.change_requests))
    fake.decide_change_request(first, approve=False)
    assert submit("feature", "room again")[0] == 202
    assert call(fake, "GET", f"/v1/change-requests/{first}")[1] == {
        "id": first,
        "kind": "bug",
        "state": "rejected",
    }


def test_event_polling_validation(fake: FakeGateway) -> None:
    assert call(fake, "GET", "/v1/events?after=-1")[0] == 400
    assert call(fake, "GET", "/v1/events?wait=61")[0] == 400
    assert call(fake, "GET", "/v1/events?after=abc")[0] == 400
    status, body = call(fake, "GET", "/v1/events?after=0")
    assert status == 200
    assert [e["kind"] for e in body["events"]] == ["accounts_changed"]
    assert call(fake, "GET", "/v1/log?limit=1001")[0] == 400


def test_llm_relay(fake: FakeGateway) -> None:
    from triage.testing.fake_gateway import FakeResponse

    assert error_code(call(fake, "POST", "/v1/llm/v1/chat/completions", {})) == "unavailable"
    seen = []

    def handler(method: str, path: str, body: bytes) -> FakeResponse:
        seen.append((method, path, body))
        return FakeResponse.json({"ok": True})

    fake.llm_handler = handler
    assert call(fake, "POST", "/v1/llm/v1/chat/completions", {"model": "m"}) == (200, {"ok": True})
    assert seen == [("POST", "/v1/chat/completions", b'{"model": "m"}')]
    assert call(fake, "DELETE", "/v1/llm/v1/models")[0] == 400


def test_direct_manipulation_is_not_logged(fake: FakeGateway) -> None:
    uid = fake.add_message("work", "INBOX", make_message())
    fake.set_flags_directly("work", "INBOX", uid, {"\\Seen"})
    new_uid = fake.move_directly("work", "INBOX", uid, "Archive")
    fake.delete_message("work", "Archive", new_uid)
    assert fake.log_entries == []


def test_reset_uid_validity_renumbers(fake: FakeGateway) -> None:
    for subject in ("a", "b", "c"):
        fake.add_message("work", "INBOX", make_message(subject=subject))
    fake.delete_message("work", "INBOX", 1)
    before = fake.folder("work", "INBOX").uid_validity
    fake.reset_uid_validity("work", "INBOX")
    folder = fake.folder("work", "INBOX")
    assert folder.uid_validity != before
    assert sorted(folder.messages) == [1, 2]
    assert folder.uid_next == 3


def test_demo_gateway_is_usable() -> None:
    fake = demo_gateway()
    status, body = call(fake, "GET", "/v1/accounts")
    assert status == 200
    assert body["accounts"][0]["id"] == "demo"
    assert len(fake.folder("demo", "INBOX").messages) == 4
    assert "Projects/Lighthouse" in fake.accounts["demo"].folders

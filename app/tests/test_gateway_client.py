"""The gateway client against the fake gateway, plus hand-made responses for the edge cases."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest

from triage import gateway as gw
from triage.gateway import GatewayClient, MessageRef, Recipient
from triage.testing.fake_gateway import FakeGateway, Fault, serve
from triage.testing.samples import ALEX, KIM, USER, make_message


def client_for(handler) -> GatewayClient:
    return GatewayClient("http://gateway.test", transport=httpx.MockTransport(handler))


def stream_client(chunks: list[bytes]) -> GatewayClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter(chunks))

    return client_for(handler)


def frame(uid: int, raw: bytes) -> bytes:
    header = {"uid": uid, "flags": [], "size": len(raw), "internal_date": "2026-11-01T12:00:00Z"}
    return json.dumps(header).encode() + b"\n" + raw + b"\n"


END = b'{"end":true,"uid_validity":7}\n'


def test_default_base_url_comes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_GATEWAY_URL", "http://example.invalid:1234/")
    with GatewayClient() as client:
        assert client.base_url == "http://example.invalid:1234"
    monkeypatch.delenv("TRIAGE_GATEWAY_URL")
    with GatewayClient() as client:
        assert client.base_url == gw.DEFAULT_GATEWAY_URL


def test_health_and_accounts(client: GatewayClient) -> None:
    assert client.health()["status"] == "ok"
    assert client.accounts() == [gw.Account("work", "robin@example.org", "Robin Tester")]


def test_folders(client: GatewayClient, fake: FakeGateway) -> None:
    folders = {f.name: f for f in client.list_folders("work")}
    assert folders["INBOX"].role == "inbox"
    assert not folders["INBOX"].protected
    assert folders["Drafts"].protected
    assert folders["Sent"].protected
    assert folders["Archive"].delimiter == "/"

    client.create_folder("work", "Triage/Newsletters")
    assert "Triage/Newsletters" in {f.name for f in client.list_folders("work")}

    fake.add_message("work", "INBOX", make_message(), flags={"\\Seen"})
    fake.add_message("work", "INBOX", make_message())
    status = client.folder_status("work", "INBOX")
    assert (status.messages, status.unseen, status.uid_next) == (2, 1, 3)
    assert status.uid_validity == fake.folder("work", "INBOX").uid_validity


def test_list_messages_pages(client: GatewayClient, fake: FakeGateway) -> None:
    when = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    for _ in range(5):
        fake.add_message("work", "INBOX", make_message(), internal_date=when)
    fake.delete_message("work", "INBOX", 2)

    page = client.list_messages("work", "INBOX", limit=3)
    assert [m.uid for m in page.messages] == [1, 3, 4]
    assert page.next_uid == 5
    assert page.messages[0].internal_date == when
    assert page.messages[0].size == len(make_message())

    pages = list(client.iter_message_pages("work", "INBOX", page_size=2))
    assert [[m.uid for m in p.messages] for p in pages] == [[1, 3], [4, 5]]
    assert pages[-1].next_uid is None

    bounded = client.list_messages("work", "INBOX", uid_min=3, uid_max=4)
    assert [m.uid for m in bounded.messages] == [3, 4]


def test_iter_pages_rejects_a_cursor_that_does_not_advance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"uid_validity": 1, "messages": [], "next_uid": 1})

    with client_for(handler) as client, pytest.raises(gw.GatewayProtocolError):
        list(client.iter_message_pages("work", "INBOX"))


def test_fetch_messages(client: GatewayClient, fake: FakeGateway) -> None:
    first = make_message(subject="one")
    second = make_message(subject="two", text="line\n" * 5000)
    fake.add_message("work", "INBOX", first, flags={"\\Seen", "$Label"})
    fake.add_message("work", "INBOX", second)

    with client.fetch_messages("work", "INBOX", [1, 2, 99]) as stream:
        messages = list(stream)
        assert stream.uid_validity == fake.folder("work", "INBOX").uid_validity
    assert [m.uid for m in messages] == [1, 2]
    assert messages[0].raw == first
    assert messages[1].raw == second
    assert messages[0].flags == ("$Label", "\\Seen")
    assert messages[0].meta.size == len(first)


def test_fetch_nothing_makes_no_request(client: GatewayClient, fake: FakeGateway) -> None:
    stream = client.fetch_messages("work", "INBOX", [])
    assert list(stream) == []
    assert ("POST", "/v1/messages/fetch") not in fake.requests


def test_fetch_too_many_is_refused_locally(client: GatewayClient) -> None:
    with pytest.raises(gw.BadRequestError):
        client.fetch_messages("work", "INBOX", list(range(1, 102)))


def test_fetch_error_response(client: GatewayClient) -> None:
    with pytest.raises(gw.NotFoundError):
        client.fetch_messages("work", "No such folder", [1])


def test_fetch_stream_is_parsed_incrementally() -> None:
    """Each message is available before the rest of the response has been produced."""
    produced: list[int] = []

    def chunks() -> Iterator[bytes]:
        for uid in (1, 2, 3):
            produced.append(uid)
            data = frame(uid, b"x" * 1000)
            # Split frames at awkward places: mid-header, mid-body, before the newline.
            yield data[:10]
            yield data[10:500]
            yield data[500:-1]
            yield data[-1:]
        yield END

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    with client_for(handler) as client, client.fetch_messages("work", "INBOX", [1, 2, 3]) as s:
        first = next(s)
        assert first.uid == 1
        assert produced == [1]
        assert [m.uid for m in s] == [2, 3]
        assert s.uid_validity == 7


def test_fetch_message_body_may_contain_anything() -> None:
    raw = b'{"end":true}\n\x00\xff\r\n{"error":{}}\n'
    with stream_client([frame(1, raw), frame(2, b""), END]) as client:
        messages = list(client.fetch_messages("work", "INBOX", [1, 2]))
    assert [m.raw for m in messages] == [raw, b""]


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [frame(1, b"abc")],
        [frame(1, b"abc")[:-3]],
        [frame(1, b"abc")[:-1]],
        [b'{"uid":1,"flags":[],"size":3'],
    ],
)
def test_fetch_detects_truncated_stream(chunks: list[bytes]) -> None:
    with stream_client(chunks) as client, pytest.raises(gw.TruncatedStreamError):
        list(client.fetch_messages("work", "INBOX", [1]))


def test_fetch_truncation_and_error_frames_from_the_fake(
    client: GatewayClient, fake: FakeGateway
) -> None:
    for _ in range(3):
        fake.add_message("work", "INBOX", make_message())

    fake.truncate_fetch_after = 2
    received = []
    with pytest.raises(gw.TruncatedStreamError):
        for message in client.fetch_messages("work", "INBOX", [1, 2, 3]):
            received.append(message.uid)
    assert received == [1, 2]

    fake.clear_faults()
    fake.fetch_error_after = (1, "unavailable")
    received = []
    with pytest.raises(gw.UnavailableError):
        for message in client.fetch_messages("work", "INBOX", [1, 2, 3]):
            received.append(message.uid)
    assert received == [1]


@pytest.mark.parametrize(
    "chunks",
    [
        [b"this is not json\n"],
        [b"[1, 2]\n"],
        [b'{"uid":1,"flags":[],"size":-1,"internal_date":"2026-11-01T12:00:00Z"}\n'],
        [b'{"uid":"1","flags":[],"size":1,"internal_date":"2026-11-01T12:00:00Z"}\nx\n'],
        [b'{"uid":1,"flags":[],"size":1,"internal_date":"yesterday"}\nx\n'],
        [frame(1, b"abc")[:-1] + b"X", END],
        [b"x" * 100_000],
    ],
)
def test_fetch_rejects_malformed_frames(chunks: list[bytes]) -> None:
    with stream_client(chunks) as client, pytest.raises(gw.GatewayProtocolError):
        list(client.fetch_messages("work", "INBOX", [1]))


def test_search(client: GatewayClient, fake: FakeGateway) -> None:
    fake.add_message("work", "INBOX", make_message(sender=ALEX, subject="Budget"), flags={"\\Seen"})
    fake.add_message("work", "INBOX", make_message(sender=KIM, subject="Lunch"))
    query = gw.q_and({"from": "alex"}, {"seen": True})
    assert client.search("work", "INBOX", query).uids == (1,)
    assert client.search("work", "INBOX", gw.q_all()).uids == (1, 2)
    assert client.search("work", "INBOX", gw.q_not({"subject": "budget"})).uids == (2,)
    assert client.search("work", "INBOX", gw.q_or({"subject": "lunch"}, {"uid": [1, 1]})).uids == (
        1,
        2,
    )
    with pytest.raises(gw.UnsupportedError):
        client.search("work", "INBOX", {"subject": "café"})


def test_flags_and_move(client: GatewayClient, fake: FakeGateway) -> None:
    fake.add_message("work", "INBOX", make_message())
    validity = fake.folder("work", "INBOX").uid_validity

    client.set_flags("work", "INBOX", validity, [1], add=["\\Seen", "$Triaged"])
    assert fake.folder("work", "INBOX").messages[1].flags == {"\\Seen", "$Triaged"}
    client.set_flags("work", "INBOX", validity, [1], remove=["$Triaged"])
    assert fake.folder("work", "INBOX").messages[1].flags == {"\\Seen"}

    with pytest.raises(gw.ForbiddenError):
        client.set_flags("work", "INBOX", validity, [1], add=["\\Deleted"])
    with pytest.raises(gw.UidValidityChangedError):
        client.set_flags("work", "INBOX", validity + 1, [1], add=["\\Seen"])

    result = client.move_messages("work", "INBOX", validity, [1, 5], "Archive")
    assert result.moved == (gw.MovedMessage(1, 1),)
    assert result.destination_uid_validity == fake.folder("work", "Archive").uid_validity
    assert not fake.folder("work", "INBOX").messages
    with pytest.raises(gw.ForbiddenError):
        client.move_messages("work", "Archive", result.destination_uid_validity or 0, [1], "Sent")


def test_drafts_and_log(client: GatewayClient, fake: FakeGateway) -> None:
    fake.add_message(
        "work", "INBOX", make_message(sender=ALEX, to=[USER], message_id="<q1@example.com>")
    )
    validity = fake.folder("work", "INBOX").uid_validity
    reference = MessageRef("INBOX", validity, 1)

    draft = client.create_draft(
        "work",
        to=[Recipient(ALEX[1], ALEX[0])],
        subject="Re: Hello",
        body="Thanks!\n",
        in_reply_to=reference,
    )
    assert draft.folder == "Drafts"
    assert draft.message_id.startswith("<")
    assert draft.message_id.endswith("@example.org>")

    with pytest.raises(gw.ForbiddenError):
        client.create_draft(
            "work",
            to=[Recipient("mallory@elsewhere.example")],
            subject="Re: Hello",
            body="Here are the secrets.",
            in_reply_to=reference,
        )

    client.set_flags("work", "INBOX", validity, [1], add=["\\Answered"])
    page = client.log()
    assert [entry.op for entry in page.entries] == ["draft", "flags"]
    assert page.last_seq == 2
    assert page.entries[0].data["in_reply_to"] == reference.to_json()
    assert page.entries[0].account == "work"
    assert client.log(after=1).entries[0].seq == 2
    assert client.log(after=2).entries == ()


def test_change_requests(client: GatewayClient, fake: FakeGateway) -> None:
    request = client.submit_change_request("feature", "Please add a dark mode.")
    assert request.state == "pending"
    assert request.kind == "feature"
    assert client.change_request(request.id).state == "pending"

    fake.decide_change_request(request.id, approve=True)
    assert client.change_request(request.id) == gw.ChangeRequest(request.id, "approved", "feature")
    events = client.events().events
    assert events[-1].kind == "change_request"
    assert events[-1].data["id"] == request.id

    with pytest.raises(gw.NotFoundError):
        client.change_request("cr-does-not-exist")
    with pytest.raises(gw.BadRequestError):
        client.submit_change_request("feature", "x" * 4001)


def test_coding_events_and_long_polling(client: GatewayClient, fake: FakeGateway) -> None:
    assert client.coding_events().events == ()
    fake.push_coding_event("text", "Working on it", "cr-1")
    page = client.coding_events()
    assert page.last_seq == 1
    assert page.events[0].text == "Working on it"
    assert page.events[0].kind == "text"
    assert client.coding_events(after=1).events == ()

    # A waiting poll returns as soon as an event arrives.
    timer = threading.Timer(0.05, lambda: fake.push_coding_event("done", "Finished", "cr-1"))
    timer.start()
    try:
        page = client.coding_events(after=1, wait=10)
    finally:
        timer.join()
    assert [e.kind for e in page.events] == ["done"]
    # A poll with nothing to report returns empty-handed after the wait.
    assert client.coding_events(after=2, wait=0.01).events == ()


@pytest.mark.parametrize(
    ("code", "status", "error_type"),
    [
        ("bad_request", 400, gw.BadRequestError),
        ("forbidden", 403, gw.ForbiddenError),
        ("not_found", 404, gw.NotFoundError),
        ("uid_validity_changed", 409, gw.UidValidityChangedError),
        ("unsupported", 501, gw.UnsupportedError),
        ("imap_error", 502, gw.ImapError),
        ("unavailable", 503, gw.UnavailableError),
    ],
)
def test_error_mapping(
    client: GatewayClient, fake: FakeGateway, code: str, status: int, error_type: type
) -> None:
    fake.add_fault(Fault(code=code, message="because"))
    with pytest.raises(error_type) as caught:
        client.list_folders("work")
    assert caught.value.code == code
    assert caught.value.status == status
    assert caught.value.message == "because"
    assert str(caught.value) == f"{code}: because"


def test_unknown_error_code_is_still_a_gateway_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, json={"error": {"code": "teapot", "message": "short"}})

    with client_for(handler) as client, pytest.raises(gw.GatewayError) as caught:
        client.accounts()
    assert type(caught.value) is gw.GatewayError
    assert caught.value.code == "teapot"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, content=b"<html>oops</html>"),
        httpx.Response(500, json={"error": "nope"}),
        httpx.Response(500, json={"error": {"message": "no code"}}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[1, 2, 3]),
        httpx.Response(200, json={"accounts": "none"}),
        httpx.Response(200, json={"accounts": [{"id": 1, "address": "a@example.org"}]}),
    ],
)
def test_malformed_responses(response: httpx.Response) -> None:
    with client_for(lambda request: response) as client, pytest.raises(gw.GatewayProtocolError):
        client.accounts()


def test_connection_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with client_for(handler) as client:
        with pytest.raises(gw.GatewayConnectionError):
            client.accounts()
        with pytest.raises(gw.GatewayConnectionError):
            client.fetch_messages("work", "INBOX", [1])


def test_environment_proxies_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    with GatewayClient("http://gateway.test") as client:
        assert not client._http.trust_env


def test_parse_timestamp() -> None:
    assert gw.parse_timestamp("2026-11-01T12:00:00Z") == datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert gw.parse_timestamp("2026-11-01T13:00:00+01:00") == datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert gw.parse_timestamp("2026-11-01T12:00:00").tzinfo is UTC
    with pytest.raises(gw.GatewayProtocolError):
        gw.parse_timestamp(12345)


def test_client_against_the_standalone_server(fake: FakeGateway) -> None:
    """The same fake served over a real socket, as it will be for UI development."""
    raw = make_message(text="x" * 200_000)
    fake.add_message("work", "INBOX", raw)
    server = serve(fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with GatewayClient(f"http://127.0.0.1:{port}") as client:
            assert [a.id for a in client.accounts()] == ["work"]
            with client.fetch_messages("work", "INBOX", [1]) as stream:
                assert [m.raw for m in stream] == [raw]
                assert stream.uid_validity == fake.folder("work", "INBOX").uid_validity
            with pytest.raises(gw.NotFoundError):
                client.folder_status("work", "Nowhere")
            fake.truncate_fetch_after = 0
            with pytest.raises(gw.TruncatedStreamError):
                list(client.fetch_messages("work", "INBOX", [1]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

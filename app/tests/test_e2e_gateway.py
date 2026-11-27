"""End to end: the app's client and sync engine against the real gateway and a real IMAP server.

Run with ``TRIAGE_GATEWAY_BIN=../trusted/target/debug/triage-gateway uv run pytest``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import real_gateway

from triage.gateway import (
    ForbiddenError,
    GatewayClient,
    MessageRef,
    NotFoundError,
    Recipient,
    UidValidityChangedError,
)
from triage.store import MessageKey, Store
from triage.sync import SyncEngine
from triage.testing.samples import ALEX, KIM, USER, make_message

pytestmark = pytest.mark.skipif(
    real_gateway.available() is not None, reason=str(real_gateway.available())
)

NOW = datetime.now(UTC)


@pytest.fixture
def server() -> Iterator[real_gateway.RealGateway]:
    gateway = real_gateway.RealGateway()
    yield gateway
    gateway.close()


@pytest.fixture
def real_client(server: real_gateway.RealGateway) -> Iterator[GatewayClient]:
    with GatewayClient(server.url) as client:
        yield client


def populate(server: real_gateway.RealGateway) -> None:
    for number in range(1, 13):
        server.deliver(
            "INBOX",
            make_message(
                sender=ALEX if number % 2 else KIM,
                subject=f"Quarterly report {number}",
                text=f"Hello Robin,\n\nplease find figure {number} for the zeppelin project.\n",
                date=NOW - timedelta(days=number),
                message_id=f"<report-{number}@example.com>",
            ),
            seen=number > 8,
        )
    server.deliver(
        "INBOX",
        make_message(
            sender=KIM,
            cc=[ALEX],
            subject="Größe des Anhangs — 添付ファイル",
            text="Grüße aus Zürich.\n",
            html='<p>Grüße aus <b>Zürich</b>.</p><img src="https://tracker.example/pixel.gif">',
            attachments=[("notes.txt", "text/plain", b"some notes")],
            date=NOW - timedelta(hours=3),
            message_id="<umlaut@example.net>",
        ),
    )
    server.deliver(
        "Archive",
        make_message(
            subject="Old but recent enough",
            text="An archived message about gardening.\n",
            date=NOW - timedelta(days=20),
            message_id="<archived@example.com>",
        ),
        seen=True,
    )


def test_sync_search_and_changes_round_trip(
    server: real_gateway.RealGateway, real_client: GatewayClient, tmp_path: Path
) -> None:
    populate(server)
    store = Store(tmp_path / "cache")
    engine = SyncEngine(real_client, store)

    # --- First sync: everything arrives, parsed and searchable.
    snapshot = engine.sync_all()
    assert snapshot.errors == []
    counts = {c.folder: c for c in store.counts("work")}
    assert counts["INBOX"].total == 13
    assert counts["INBOX"].unread == 9
    assert counts["Archive"].total == 1

    hits = store.search("zeppelin", account="work")
    assert len(hits) == 12
    umlaut = store.search("Zürich", account="work")
    assert [m.subject for m in umlaut] == ["Größe des Anhangs — 添付ファイル"]
    parsed = store.get_parsed(umlaut[0].key)
    assert parsed is not None
    assert parsed.blocked_remote_content
    assert parsed.html is not None
    assert "tracker.example" not in parsed.html.replace('data-triage-blocked-src=""', "")
    assert [a.filename for a in parsed.attachments] == ["notes.txt"]
    assert store.get_raw(umlaut[0].key) is not None

    # Fetching did not mark anything as read on the server.
    assert real_client.folder_status("work", "INBOX").unseen == 9

    # --- Changes through the gateway, then a second sync picks them up.
    inbox = store.folder("work", "INBOX")
    assert inbox is not None
    validity = inbox.uid_validity
    target = store.search("report 1", account="work", folder="INBOX")
    first = next(m for m in target if m.subject == "Quarterly report 1")
    second = next(m for m in store.search("report", account="work") if m.subject.endswith(" 2"))

    real_client.set_flags("work", "INBOX", validity, [first.key.uid], add=["\\Seen", "$Triaged"])
    moved = real_client.move_messages("work", "INBOX", validity, [second.key.uid], "Archive")
    assert [m.uid for m in moved.moved] == [second.key.uid]
    assert moved.moved[0].new_uid is not None

    draft = real_client.create_draft(
        "work",
        to=[Recipient(address=first.from_[0].address, name=first.from_[0].name)],
        subject="Re: Quarterly report 1",
        body="Thank you, received.\n",
        in_reply_to=MessageRef(folder="INBOX", uid_validity=validity, uid=first.key.uid),
    )
    assert draft.folder == "Drafts"

    snapshot = engine.sync_all()
    assert snapshot.errors == []
    refreshed = store.get_message(first.key)
    assert refreshed is not None
    assert refreshed.seen
    assert "$Triaged" in refreshed.flags
    assert store.get_message(second.key) is None, "the moved message left the inbox"
    archive = store.folder("work", "Archive")
    assert archive is not None
    moved_key = MessageKey("work", "Archive", archive.uid_validity, moved.moved[0].new_uid)
    moved_message = store.get_message(moved_key)
    assert moved_message is not None
    assert moved_message.subject == "Quarterly report 2"
    counts = {c.folder: c for c in store.counts("work")}
    assert counts["INBOX"].total == 12
    assert counts["Archive"].total == 2
    assert counts["Drafts"].total == 1

    # The draft is a plain-text reply that the gateway composed.
    drafts_folder = store.folder("work", "Drafts")
    assert drafts_folder is not None
    stored_draft = store.list_folder("work", "Drafts")[0]
    assert stored_draft.message_id == draft.message_id.strip("<>") or (
        stored_draft.message_id == draft.message_id
    )
    draft_parsed = store.get_parsed(stored_draft.key)
    assert draft_parsed is not None
    assert draft_parsed.in_reply_to in ("<report-1@example.com>", "report-1@example.com")
    assert "Thank you, received." in draft_parsed.text
    assert draft_parsed.html is None
    assert [a.address for a in draft_parsed.from_] == [USER[1]]

    # --- The operation log recorded the three changes.
    log = real_client.log()
    assert [entry.op for entry in log.entries] == ["flags", "move", "draft"]

    # --- The gateway's refusals arrive as the matching exceptions.
    with pytest.raises(ForbiddenError):
        real_client.set_flags("work", "INBOX", validity, [first.key.uid], add=["\\Deleted"])
    with pytest.raises(ForbiddenError):
        real_client.move_messages("work", "INBOX", validity, [first.key.uid], "Drafts")
    with pytest.raises(ForbiddenError):
        real_client.create_draft(
            "work",
            to=[Recipient(address="mallory@evil.example")],
            subject="Re: Quarterly report 1",
            body="x",
            in_reply_to=MessageRef(folder="INBOX", uid_validity=validity, uid=first.key.uid),
        )
    with pytest.raises(UidValidityChangedError):
        real_client.set_flags("work", "INBOX", validity + 1, [first.key.uid], add=["\\Seen"])
    with pytest.raises(NotFoundError):
        real_client.folder_status("work", "No such folder")
    assert len(real_client.log().entries) == 3, "refused requests are not logged"

    store.close()


def test_change_requests_and_events(real_client: GatewayClient) -> None:
    request = real_client.submit_change_request("feature", "Please add a dark mode.")
    assert request.state == "pending"
    assert real_client.change_request(request.id).state == "pending"
    with pytest.raises(ForbiddenError):
        real_client.submit_change_request("bug", "hidden​text")
    assert not real_client.coding_events(after=0, wait=0).events
    assert [a.id for a in real_client.accounts()] == ["work"]
    assert real_client.health()["status"] == "ok"

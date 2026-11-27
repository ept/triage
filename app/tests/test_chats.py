from __future__ import annotations

import time
from pathlib import Path

import pytest

from triage.chats import ChatStore, decode_key, encode_key
from triage.store import MessageKey


@pytest.fixture
def chats(tmp_path: Path) -> ChatStore:
    return ChatStore(tmp_path)


def test_keys_round_trip_and_reject_garbage() -> None:
    key = MessageKey("work", "Projekte/Büro & Co", 1700000000, 42)
    encoded = encode_key(key)
    assert encoded.isascii()
    assert "/" not in encoded
    assert "=" not in encoded
    assert decode_key(encoded) == key
    for bad in [
        "",
        "!!!",
        "bm90IGpzb24",
        encode_key(key)[:-3],
        "WzEsMiwzXQ",
        "W3RydWUsICJhIiwgMSwgMl0",
    ]:
        assert decode_key(bad) is None, bad


def test_conversations_and_items(chats: ChatStore, tmp_path: Path) -> None:
    first = chats.create()
    second = chats.create("Second")
    # Times are stored in milliseconds, and conversations used in the same millisecond are
    # listed newest first; make sure that the first one really is used later than that.
    time.sleep(0.005)
    item = chats.add_item(first.id, "user", "Hello")
    chats.add_item(
        first.id,
        "tool",
        tool={"name": "search_mail", "summary": "Searched", "ok": True},
        messages=[{"key": "k", "handle": "m1"}],
        llm={"step": 1, "content": "hidden"},
    )
    chats.set_title(first.id, "  A   title\nwith breaks  ")

    listed = chats.conversations()
    assert [c.id for c in listed] == [first.id, second.id], "most recently used first"
    assert listed[0].title == "A title with breaks"
    assert chats.conversation(999) is None

    items = chats.items(first.id)
    assert [i.role for i in items] == ["user", "tool"]
    assert items[0].id == item.id
    as_json = items[1].to_json()
    assert as_json["tool"]["name"] == "search_mail"
    assert as_json["messages"][0]["handle"] == "m1"
    assert "llm" not in as_json, "the model's view of an item is not sent to the interface"
    assert items[1].llm == {"step": 1, "content": "hidden"}

    chats.update_attachments(items[1].id, change_request={"id": "cr-1", "state": "approved"})
    assert chats.items(first.id)[1].change_request == {"id": "cr-1", "state": "approved"}

    # Everything survives reopening.
    chats.close()
    reopened = ChatStore(tmp_path)
    assert len(reopened.items(first.id)) == 2


def test_handles_are_stable_per_conversation(chats: ChatStore) -> None:
    one = chats.create()
    two = chats.create()
    a = MessageKey("work", "INBOX", 1, 10)
    b = MessageKey("work", "INBOX", 1, 11)
    assert chats.handle_for(one.id, a) == "m1"
    assert chats.handle_for(one.id, b) == "m2"
    assert chats.handle_for(one.id, a) == "m1"
    assert chats.handle_for(two.id, b) == "m1"
    assert chats.key_for(one.id, "m2") == b
    assert chats.key_for(one.id, "[M2]") == b
    assert chats.key_for(two.id, "m2") is None
    for bad in ["", "2", "mx", "m", "m-1", "m1; drop table"]:
        assert chats.key_for(one.id, bad) is None


def test_undone_operations_are_remembered(chats: ChatStore) -> None:
    chats.mark_undone(3)
    chats.mark_undone(3)
    chats.mark_undone(5)
    assert chats.undone() == {3, 5}

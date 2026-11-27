from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from triage.mime import Address, parse_message
from triage.store import (
    INDEX_FILE,
    RAW_FILE,
    MessageKey,
    NewMessage,
    StaleFolderError,
    Store,
    StoreError,
    fts_query,
)
from triage.testing.samples import ALEX, KIM, NEWSLETTER, USER, make_message

T0 = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
VALIDITY = 77


def new(uid: int, raw: bytes, *, flags=(), when: datetime = T0, parse: bool = True) -> NewMessage:
    return NewMessage(
        uid=uid,
        flags=flags,
        internal_date=when,
        raw=raw,
        parsed=parse_message(raw) if parse else None,
    )


def key(uid: int, folder: str = "INBOX", account: str = "work") -> MessageKey:
    return MessageKey(account, folder, VALIDITY, uid)


@pytest.fixture
def filled(store: Store) -> Store:
    for account in ("work", "home"):
        for folder, role in (("INBOX", "inbox"), ("Sent", "sent"), ("Archive", "archive")):
            store.upsert_folder(account, folder, role=role, delimiter="/")
            store.reset_folder(account, folder, VALIDITY)
    store.add_messages(
        "work",
        "INBOX",
        VALIDITY,
        [
            new(
                1,
                make_message(
                    sender=KIM,
                    subject="Lighthouse schedule",
                    text="Could you check the dates of the lighthouse project?\n",
                    message_id="<l1@example.net>",
                    date=T0,
                ),
                flags=["\\Seen"],
                when=T0,
            ),
            new(
                2,
                make_message(
                    sender=KIM,
                    subject="Re: Lighthouse schedule",
                    text="Week three works. Zürich café résumé.\n",
                    message_id="<l3@example.net>",
                    in_reply_to="<l2@example.org>",
                    references=["<l1@example.net>", "<l2@example.org>"],
                    date=T0 + timedelta(days=2),
                ),
                when=T0 + timedelta(days=2),
            ),
            new(
                3,
                make_message(
                    sender=NEWSLETTER,
                    subject="This week in examples",
                    text="Ten new examples about lighthouses.\n",
                    html="<p>Ten new <b>examples</b></p>",
                    message_id="<w42@newsletter.example>",
                    date=T0 + timedelta(days=3),
                ),
                when=T0 + timedelta(days=3),
            ),
            new(
                4,
                make_message(
                    sender=ALEX,
                    cc=[KIM],
                    subject="Invoice 2026-117",
                    text="Payment is due next week.\n",
                    message_id="<inv117@example.com>",
                    date=T0 + timedelta(days=4),
                    attachments=[("invoice.pdf", "application/pdf", b"%PDF synthetic")],
                ),
                flags=["\\Flagged"],
                when=T0 + timedelta(days=4),
            ),
        ],
    )
    store.add_messages(
        "work",
        "Sent",
        VALIDITY,
        [
            new(
                1,
                make_message(
                    sender=USER,
                    to=[KIM],
                    subject="Re: Lighthouse schedule",
                    text="Week two clashes with the workshop.\n",
                    message_id="<l2@example.org>",
                    in_reply_to="<l1@example.net>",
                    references=["<l1@example.net>"],
                    date=T0 + timedelta(days=1),
                ),
                flags=["\\Seen"],
                when=T0 + timedelta(days=1),
            )
        ],
    )
    store.add_messages(
        "home",
        "INBOX",
        VALIDITY,
        [
            new(
                1,
                make_message(
                    sender=ALEX,
                    subject="Lighthouse holiday",
                    text="Shall we visit the lighthouse?\n",
                    message_id="<h1@example.com>",
                    references=["<l1@example.net>"],
                ),
            )
        ],
    )
    return store


# -- schema and files ------------------------------------------------------------------------


def test_creates_two_databases_in_wal_mode(store: Store) -> None:
    assert (store.cache_dir / INDEX_FILE).exists()
    assert (store.cache_dir / RAW_FILE).exists()
    for name in (INDEX_FILE, RAW_FILE):
        db = sqlite3.connect(store.cache_dir / name)
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        db.close()


def test_cache_dir_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_CACHE_DIR", str(tmp_path / "from-env"))
    with Store() as store:
        assert store.cache_dir == tmp_path / "from-env"
        assert (tmp_path / "from-env" / INDEX_FILE).exists()


def test_reopening_keeps_data_and_does_not_migrate_again(tmp_path: Path) -> None:
    with Store(tmp_path) as store:
        store.upsert_folder("work", "INBOX", role="inbox")
        store.reset_folder("work", "INBOX", VALIDITY)
        store.add_messages("work", "INBOX", VALIDITY, [new(1, make_message(subject="Kept"))])
    with Store(tmp_path) as store:
        message = store.get_message(key(1))
        assert message is not None
        assert message.subject == "Kept"


def test_refuses_a_database_from_the_future(tmp_path: Path) -> None:
    Store(tmp_path).close()
    db = sqlite3.connect(tmp_path / INDEX_FILE)
    db.execute("PRAGMA user_version = 99")
    db.close()
    with pytest.raises(StoreError, match="schema version 99"):
        Store(tmp_path)


def test_migrations_run_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import triage.store as store_module

    Store(tmp_path).close()

    def add_column(db: sqlite3.Connection) -> None:
        db.execute("ALTER TABLE folders ADD COLUMN note TEXT")

    def failing(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(
        store_module, "INDEX_MIGRATIONS", (*store_module.INDEX_MIGRATIONS, add_column, failing)
    )
    with pytest.raises(RuntimeError, match="migration failed"):
        Store(tmp_path)
    db = sqlite3.connect(tmp_path / INDEX_FILE)
    # The good migration was committed; the failed one left nothing behind.
    assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert "note" in [row[1] for row in db.execute("PRAGMA table_info(folders)")]
    assert (
        db.execute("SELECT count(*) FROM sqlite_master WHERE name = 'half_done'").fetchone()[0] == 0
    )
    db.close()


def test_closed_store_cannot_be_used(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.close()
    with pytest.raises(StoreError):
        store.folders()
    store.close()  # closing twice is harmless


# -- folders ---------------------------------------------------------------------------------


def test_folders(store: Store) -> None:
    assert store.folder("work", "INBOX") is None
    store.upsert_folder("work", "INBOX", role="inbox", delimiter="/")
    store.upsert_folder("work", "Shared", selectable=False)
    store.upsert_folder("home", "INBOX", role="inbox")
    state = store.folder("work", "INBOX")
    assert state is not None
    assert (state.role, state.delimiter, state.selectable, state.protected) == (
        "inbox",
        "/",
        True,
        False,
    )
    assert (state.uid_validity, state.uid_next, state.synced_at) == (None, None, None)

    store.upsert_folder("work", "INBOX", role="archive", protected=True)
    state = store.folder("work", "INBOX")
    assert state is not None
    assert (state.role, state.protected) == ("archive", True)

    assert [f.name for f in store.folders("work")] == ["INBOX", "Shared"]
    assert len(store.folders()) == 3
    assert store.accounts() == ["home", "work"]

    store.reset_folder("work", "INBOX", VALIDITY)
    store.mark_folder_synced("work", "INBOX", VALIDITY, uid_next=12, when=T0)
    state = store.folder("work", "INBOX")
    assert state is not None
    assert (state.uid_validity, state.uid_next, state.synced_at) == (VALIDITY, 12, T0)

    store.remove_folder("work", "Shared")
    store.remove_folder("work", "Never existed")
    assert [f.name for f in store.folders("work")] == ["INBOX"]


def test_writes_need_the_current_uid_validity(store: Store) -> None:
    message = new(1, make_message())
    with pytest.raises(StaleFolderError):
        store.add_messages("work", "INBOX", VALIDITY, [message])  # unknown folder
    store.upsert_folder("work", "INBOX")
    with pytest.raises(StaleFolderError):
        store.add_messages("work", "INBOX", VALIDITY, [message])  # no uid_validity yet
    store.reset_folder("work", "INBOX", VALIDITY)
    store.add_messages("work", "INBOX", VALIDITY, [message])
    for operation in (
        lambda: store.add_messages("work", "INBOX", VALIDITY + 1, [message]),
        lambda: store.update_flags("work", "INBOX", VALIDITY + 1, {1: ["\\Seen"]}),
        lambda: store.remove_messages("work", "INBOX", VALIDITY + 1, [1]),
        lambda: store.mark_folder_synced("work", "INBOX", VALIDITY + 1),
    ):
        with pytest.raises(StaleFolderError):
            operation()
    with pytest.raises(StaleFolderError):
        store.reset_folder("work", "Nope", 1)
    assert store.get_message(key(1)) is not None
    # A key with an outdated uid_validity finds nothing.
    assert store.get_message(MessageKey("work", "INBOX", VALIDITY + 1, 1)) is None
    assert store.get_raw(MessageKey("work", "INBOX", VALIDITY + 1, 1)) is None


def test_reset_folder_drops_its_messages(filled: Store) -> None:
    filled.reset_folder("work", "INBOX", VALIDITY + 1)
    assert filled.cached_flags("work", "INBOX") == {}
    assert filled.search("lighthouse", account="work", folder="INBOX") == []
    state = filled.folder("work", "INBOX")
    assert state is not None
    assert state.uid_validity == VALIDITY + 1
    assert len(filled.cached_flags("work", "Sent")) == 1


# -- messages --------------------------------------------------------------------------------


def test_get_message(filled: Store) -> None:
    message = filled.get_message(key(4))
    assert message is not None
    assert message.key == key(4)
    assert message.subject == "Invoice 2026-117"
    assert message.from_ == (Address(*ALEX),)
    assert message.to == (Address(*USER),)
    assert message.cc == (Address(*KIM),)
    assert message.flags == ("\\Flagged",)
    assert message.flagged
    assert not message.seen
    assert not message.answered
    assert message.message_id == "<inv117@example.com>"
    assert message.body == "Payment is due next week."
    assert message.preview == "Payment is due next week."
    assert message.timestamp == T0 + timedelta(days=4)
    assert message.attachment_count == 1
    assert [a.filename for a in message.attachments] == ["invoice.pdf"]
    assert not message.has_html
    assert message.parse_error is None
    assert len(message.content_hash) == 32

    reply = filled.get_message(key(2))
    assert reply is not None
    assert reply.in_reply_to == "<l2@example.org>"
    assert reply.references == ("<l1@example.net>", "<l2@example.org>")
    newsletter = filled.get_message(key(3))
    assert newsletter is not None
    assert newsletter.has_html
    assert filled.get_message(key(99)) is None


def test_raw_round_trip_and_parsed_form(filled: Store) -> None:
    raw = make_message(html="<p>Ten new <b>examples</b></p>", text="Ten new examples\n")
    filled.add_messages("work", "Archive", VALIDITY, [new(5, raw)])
    assert filled.get_raw(key(5, "Archive")) == raw
    parsed = filled.get_parsed(key(5, "Archive"))
    assert parsed is not None
    assert parsed.html is not None
    assert "<b>examples</b>" in parsed.html
    assert filled.get_raw(key(99)) is None
    assert filled.get_parsed(key(99)) is None


def test_raw_is_compressed_and_deduplicated(store: Store) -> None:
    for folder in ("INBOX", "Archive"):
        store.upsert_folder("work", folder)
        store.reset_folder("work", folder, VALIDITY)
    compressible = make_message(text="the same line again\n" * 5000)
    incompressible = bytes(range(256)) * 2 + b"\x00\xff"  # not a message at all, and tiny
    store.add_messages("work", "INBOX", VALIDITY, [new(1, compressible), new(2, incompressible)])
    store.add_messages("work", "Archive", VALIDITY, [new(7, compressible)])
    stats = store.stats()
    assert stats.messages == 3
    assert stats.raw_messages == 2
    assert stats.raw_bytes == len(compressible) + len(incompressible)
    assert stats.stored_bytes < len(compressible) // 10 + len(incompressible) + 100
    assert store.get_raw(key(1)) == compressible
    assert store.get_raw(key(2)) == incompressible
    assert store.get_raw(key(7, "Archive")) == compressible


def test_adding_the_same_uid_again_replaces_the_message(filled: Store) -> None:
    replacement = make_message(subject="Replaced entirely", text="fresh text\n")
    filled.add_messages("work", "INBOX", VALIDITY, [new(1, replacement, flags=["$New"])])
    message = filled.get_message(key(1))
    assert message is not None
    assert (message.subject, message.flags) == ("Replaced entirely", ("$New",))
    assert filled.search("dates", account="work") == []
    assert [m.key.uid for m in filled.search("fresh", account="work")] == [1]
    assert len(filled.cached_flags("work", "INBOX")) == 4


def test_message_without_parsed_form_is_stored_raw(store: Store) -> None:
    store.upsert_folder("work", "INBOX")
    store.reset_folder("work", "INBOX", VALIDITY)
    raw = make_message(subject="Unparsed")
    unparsed = NewMessage(1, ["\\Seen"], T0, raw, parsed=None, parse_error="ValueError: bad")
    store.add_messages("work", "INBOX", VALIDITY, [unparsed])
    message = store.get_message(key(1))
    assert message is not None
    assert (message.subject, message.body, message.parse_error) == ("", "", "ValueError: bad")
    assert message.timestamp == T0
    assert store.get_raw(key(1)) == raw


def test_add_messages_is_transactional(filled: Store) -> None:
    def messages():
        yield new(10, make_message(subject="first of batch"))
        raise RuntimeError("stream broke")

    with pytest.raises(RuntimeError):
        filled.add_messages("work", "INBOX", VALIDITY, messages())
    assert filled.get_message(key(10)) is None
    assert filled.add_messages("work", "INBOX", VALIDITY, [new(10, make_message())]) == 1


def test_flags_and_removal(filled: Store) -> None:
    assert filled.cached_flags("work", "INBOX") == {
        1: ("\\Seen",),
        2: (),
        3: (),
        4: ("\\Flagged",),
    }
    filled.update_flags(
        "work", "INBOX", VALIDITY, {2: ["\\Seen", "\\Answered", "\\Seen"], 99: ["x"]}
    )
    message = filled.get_message(key(2))
    assert message is not None
    assert message.flags == ("\\Answered", "\\Seen")
    assert message.seen
    assert message.answered
    assert [m.key.uid for m in filled.list_folder("work", "INBOX", unread=True)] == [4, 3]

    filled.remove_messages("work", "INBOX", VALIDITY, [1, 2, 99])
    assert sorted(filled.cached_flags("work", "INBOX")) == [3, 4]
    assert filled.search("schedule", account="work", folder="INBOX") == []
    assert filled.get_message(key(1)) is None


def test_timestamp_distrusts_dates_from_the_future(store: Store) -> None:
    store.upsert_folder("work", "INBOX")
    store.reset_folder("work", "INBOX", VALIDITY)
    arrival = T0
    store.add_messages(
        "work",
        "INBOX",
        VALIDITY,
        [
            new(1, make_message(date=arrival - timedelta(hours=3)), when=arrival),
            new(2, make_message(date=arrival + timedelta(days=400)), when=arrival),
            new(3, b"Subject: no date\r\n\r\nx", when=arrival),
        ],
    )
    stamps = {m.key.uid: m for m in store.list_folder("work", "INBOX")}
    assert stamps[1].timestamp == arrival - timedelta(hours=3)
    assert stamps[2].timestamp == arrival
    assert stamps[2].date == arrival + timedelta(days=400)
    assert stamps[3].timestamp == arrival
    assert stamps[3].date is None


def test_list_folder(filled: Store) -> None:
    assert [m.key.uid for m in filled.list_folder("work", "INBOX")] == [4, 3, 2, 1]
    assert [m.key.uid for m in filled.list_folder("work", "INBOX", newest_first=False)] == [
        1,
        2,
        3,
        4,
    ]
    assert [m.key.uid for m in filled.list_folder("work", "INBOX", limit=2, offset=1)] == [3, 2]
    assert [m.key.uid for m in filled.list_folder("work", "INBOX", unread=False)] == [1]
    assert filled.list_folder("work", "Archive") == []
    assert filled.list_folder("nobody", "INBOX") == []
    summary = filled.list_folder("work", "INBOX", limit=1)[0]
    assert summary.subject == "Invoice 2026-117"
    assert summary.preview == "Payment is due next week."
    assert summary.attachment_count == 1
    assert summary.snippet is None


def test_preview_is_short_and_on_one_line(store: Store) -> None:
    store.upsert_folder("work", "INBOX")
    store.reset_folder("work", "INBOX", VALIDITY)
    store.add_messages(
        "work", "INBOX", VALIDITY, [new(1, make_message(text="word\n\n   next\t" * 500))]
    )
    preview = store.list_folder("work", "INBOX")[0].preview
    assert len(preview) <= 240
    assert preview.startswith("word next word next")


# -- search ----------------------------------------------------------------------------------


def uids(results) -> list[tuple[str, str, int]]:
    return [(m.key.account, m.key.folder, m.key.uid) for m in results]


def test_search_full_text(filled: Store) -> None:
    hits = filled.search("lighthouse", account="work", order="date")
    assert uids(hits) == [
        ("work", "INBOX", 3),
        ("work", "INBOX", 2),
        ("work", "Sent", 1),
        ("work", "INBOX", 1),
    ]
    # Subject matches rank above body matches.
    ranked = filled.search("lighthouse", account="work")
    assert ranked[-1].key.uid == 3
    assert all(hit.snippet for hit in ranked)
    assert "lighthouse" in (filled.search("dates lighthouse")[0].snippet or "").lower()

    assert uids(filled.search("lighthouse")) != []
    assert len(filled.search("lighthouse")) == 5  # both accounts
    assert uids(filled.search("lighthouse", account="home")) == [("home", "INBOX", 1)]
    assert uids(filled.search("lighthouse", account="work", folder="Sent")) == [("work", "Sent", 1)]
    assert filled.search("xyzzy") == []


def test_search_syntax(filled: Store) -> None:
    def find(query: str) -> list[int]:
        return sorted(m.key.uid for m in filled.search(query, account="work", folder="INBOX"))

    assert find("week") == [2, 3, 4]
    assert find("week three") == [2]  # all words must match
    assert find('"next week"') == [4]
    assert find('"week next"') == []
    assert find("body:examp*") == [3]
    assert find("examp*") == [1, 2, 3, 4]  # every address is at an example domain
    assert find("lighthouses") == [1, 2, 3]  # words are stemmed
    assert find("zurich cafe resume") == [2]  # diacritics are ignored
    assert find("ZÜRICH") == [2]
    assert find("subject:lighthouse") == [1, 2]
    assert find("body:lighthouse") == [1, 3]
    assert find("from:kim") == [1, 2]
    assert find("from:alex") == [4]
    assert find("to:kim") == [4]  # Cc counts as a recipient
    assert find('subject:"lighthouse schedule" week') == [2]
    assert find("kim@example.net") == [1, 2, 4]
    assert find("2026-117") == [4]
    assert find("unknownprefix:week") == []


@pytest.mark.parametrize(
    "query",
    [
        '"', '""', '"unbalanced', "AND", "OR", "NOT", "NEAR(", "a AND", "(", ")", "*", "^", "-",
        "a:b:c", ":", "subject:", 'subject:"', "{x}", "a - b", "a + b", "col:*", "'; DROP TABLE messages; --",
        "\x00", "week\x00", "!!!", "a" * 5000, "x " * 500, "🙂", "%", "_",
    ],
)  # fmt: skip
def test_search_accepts_any_input(filled: Store, query: str) -> None:
    filled.search(query)
    filled.search(query, sender=query, account=query, folder=query)
    assert len(filled.search("lighthouse")) == 5  # and nothing was damaged


def test_fts_query_translation() -> None:
    assert fts_query("") is None
    assert fts_query("   ") is None
    assert fts_query("--- !!! ***") is None
    assert fts_query("hello world") == '"hello" AND "world"'
    assert fts_query('"exact phrase" other') == '"exact phrase" AND "other"'
    assert fts_query("pre*") == '"pre"*'
    assert fts_query('say "hi') == '"say" AND "hi"'
    assert fts_query("from:kim SUBJECT:x") == 'sender : "kim" AND subject : "x"'
    assert fts_query("https://ok.example/a") == '"https://ok.example/a"'
    assert fts_query('a"b') == '"a b"'
    assert fts_query("AND OR NOT") == '"AND" AND "OR" AND "NOT"'
    assert len((fts_query("w " * 100) or "").split(" AND ")) == 32


def test_search_filters(filled: Store) -> None:
    def find(**filters) -> list[int]:
        return [m.key.uid for m in filled.search("", account="work", folder="INBOX", **filters)]

    assert find() == [4, 3, 2, 1]  # no query: newest first
    assert find(unread=True) == [4, 3, 2]
    assert find(unread=False) == [1]
    assert find(flagged=True) == [4]
    assert find(flagged=False) == [3, 2, 1]
    assert find(has_attachments=True) == [4]
    assert find(has_attachments=False) == [3, 2, 1]
    assert find(sender="kim") == [2, 1]
    assert find(sender="KIM@EXAMPLE.NET") == [2, 1]
    assert find(sender="Placeholder") == [2, 1]
    assert find(sender="%") == []
    assert find(sender="_") == []
    assert find(since=T0 + timedelta(days=2)) == [4, 3, 2]
    assert find(before=T0 + timedelta(days=2)) == [1]
    assert find(since=T0 + timedelta(days=2), before=T0 + timedelta(days=4)) == [3, 2]
    assert find(limit=2) == [4, 3]
    assert find(limit=2, offset=2) == [2, 1]
    assert find(limit=0) == []

    combined = filled.search("lighthouse", account="work", sender="kim", unread=True)
    assert uids(combined) == [("work", "INBOX", 2)]
    assert len(filled.search("")) == 6


# -- conversations ---------------------------------------------------------------------------


def test_conversation(filled: Store) -> None:
    expected = [("work", "INBOX", 1), ("work", "Sent", 1), ("work", "INBOX", 2)]
    for start in (key(1), key(1, "Sent"), key(2)):
        assert uids(filled.conversation(start)) == expected
    # Unrelated messages form a conversation of one; other accounts are never mixed in,
    # even though the message in "home" refers to the same Message-ID.
    assert uids(filled.conversation(key(4))) == [("work", "INBOX", 4)]
    assert uids(filled.conversation(key(1, account="home"))) == [("home", "INBOX", 1)]
    assert filled.conversation(key(99)) == []
    assert uids(filled.conversation(key(1), limit=2)) == expected[:2]


def test_conversation_follows_chains_transitively(store: Store) -> None:
    store.upsert_folder("work", "INBOX")
    store.reset_folder("work", "INBOX", VALIDITY)
    chain = []
    for index in range(1, 31):
        # Each message only names its direct predecessor, so the chain must be walked.
        chain.append(
            new(
                index,
                make_message(
                    subject=f"step {index}",
                    message_id=f"<step{index}@example.org>",
                    in_reply_to=f"<step{index - 1}@example.org>" if index > 1 else None,
                ),
                when=T0 + timedelta(minutes=index),
            )
        )
    chain.append(new(100, make_message(subject="no ids at all")))
    chain.append(new(101, make_message(subject="also no ids")))
    store.add_messages("work", "INBOX", VALIDITY, chain)
    # The search is bounded: sixteen rounds reach sixteen hops in each direction.
    found = [m.key.uid for m in store.conversation(key(15))]
    assert found == list(range(1, 31))
    assert [m.key.uid for m in store.conversation(key(100))] == [100]


def test_conversation_with_many_references(store: Store) -> None:
    store.upsert_folder("work", "INBOX")
    store.reset_folder("work", "INBOX", VALIDITY)
    root = new(1, make_message(message_id="<root@example.org>"))
    replies = [
        new(
            uid,
            make_message(
                message_id=f"<r{uid}@example.org>",
                references=[f"<x{n}@example.org>" for n in range(150)] + ["<root@example.org>"],
            ),
        )
        for uid in range(2, 1202)
    ]
    store.add_messages("work", "INBOX", VALIDITY, [root, *replies])
    assert len(store.conversation(key(1), limit=5000)) == 1201
    assert len(store.conversation(key(1))) == 200


# -- counts and analysis ---------------------------------------------------------------------


def test_counts(filled: Store) -> None:
    counts = {(c.account, c.folder): (c.total, c.unread, c.role) for c in filled.counts()}
    assert counts[("work", "INBOX")] == (4, 3, "inbox")
    assert counts[("work", "Sent")] == (1, 0, "sent")
    assert counts[("work", "Archive")] == (0, 0, "archive")
    assert counts[("home", "INBOX")] == (1, 1, "inbox")
    assert {c.account for c in filled.counts("home")} == {"home"}


def test_analysis(filled: Store) -> None:
    pending = filled.unanalysed("v1", account="work")
    assert uids(pending) == [
        ("work", "INBOX", 4),
        ("work", "INBOX", 3),
        ("work", "INBOX", 2),
        ("work", "Sent", 1),
        ("work", "INBOX", 1),
    ]
    assert filled.get_analysis(key(4)) is None
    assert filled.set_analysis(key(4), "v1", {"needs_reply": True, "summary": "Invoice due"})
    assert not filled.set_analysis(key(99), "v1", {})
    analysis = filled.get_analysis(key(4))
    assert analysis is not None
    assert (analysis.analyser, analysis.result) == (
        "v1",
        {"needs_reply": True, "summary": "Invoice due"},
    )
    assert analysis.created_at.tzinfo is not None

    assert ("work", "INBOX", 4) not in uids(filled.unanalysed("v1"))
    assert uids(filled.unanalysed("v1", account="work", folder="INBOX", limit=2)) == [
        ("work", "INBOX", 3),
        ("work", "INBOX", 2),
    ]
    assert len(filled.unanalysed("v1")) == 5
    # A new analyser version makes everything due again; a new result replaces the old one.
    assert ("work", "INBOX", 4) in uids(filled.unanalysed("v2"))
    filled.set_analysis(key(4), "v2", {"summary": "better"})
    updated = filled.get_analysis(key(4))
    assert updated is not None
    assert updated.result == {"summary": "better"}


def test_analysis_follows_a_message_to_another_folder(filled: Store) -> None:
    filled.set_analysis(key(4), "v1", {"summary": "Invoice due"})
    raw = filled.get_raw(key(4))
    assert raw is not None
    # The message is moved: it appears in the archive under a new UID and leaves the inbox.
    filled.add_messages("work", "Archive", VALIDITY, [new(50, raw, when=T0 + timedelta(days=4))])
    filled.remove_messages("work", "INBOX", VALIDITY, [4])
    moved = filled.get_analysis(key(50, "Archive"))
    assert moved is not None
    assert moved.result == {"summary": "Invoice due"}
    assert ("work", "Archive", 50) not in uids(filled.unanalysed("v1"))
    # The same content in another account has its own analysis.
    filled.add_messages("home", "INBOX", VALIDITY, [new(60, raw)])
    assert filled.get_analysis(key(60, account="home")) is None


def test_unanalysed_returns_duplicates_once(filled: Store) -> None:
    raw = filled.get_raw(key(3))
    assert raw is not None
    filled.add_messages("work", "Archive", VALIDITY, [new(9, raw, when=T0 + timedelta(days=3))])
    pending = uids(filled.unanalysed("v1", account="work"))
    assert len(pending) == 5
    assert (
        sum(1 for entry in pending if entry in (("work", "INBOX", 3), ("work", "Archive", 9))) == 1
    )


# -- housekeeping ----------------------------------------------------------------------------


def test_remove_account(filled: Store) -> None:
    filled.set_analysis(key(1), "v1", {"x": 1})
    filled.set_analysis(key(1, account="home"), "v1", {"x": 2})
    filled.remove_account("work")
    assert filled.accounts() == ["home"]
    assert filled.search("lighthouse", account="work") == []
    assert filled.get_analysis(key(1, account="home")) is not None
    assert filled.check_consistency() == (0, 5)  # the raw messages of "work" are collected
    assert filled.stats().raw_messages == 1
    index = sqlite3.connect(filled.cache_dir / INDEX_FILE)
    assert index.execute("SELECT count(*) FROM analysis").fetchone()[0] == 1
    assert index.execute("SELECT count(*) FROM message_refs").fetchone()[0] == 2
    index.close()


def test_check_consistency_repairs_both_directions(filled: Store) -> None:
    assert filled.check_consistency() == (0, 0)
    filled.set_analysis(key(2), "v1", {"x": 1})
    raw_db = sqlite3.connect(filled.cache_dir / RAW_FILE)
    message = filled.get_message(key(1))
    assert message is not None
    # Simulate a crash that lost one raw message...
    raw_db.execute("DELETE FROM raw_messages WHERE hash = ?", (message.content_hash,))
    # ...and left one behind that nothing refers to.
    raw_db.execute(
        "INSERT INTO raw_messages (hash, size, encoding, data) VALUES (?, 1, 0, ?)",
        (b"h" * 32, b"x"),
    )
    raw_db.commit()
    raw_db.close()
    assert filled.get_raw(key(1)) is None

    assert filled.check_consistency() == (1, 1)
    assert filled.get_message(key(1)) is None  # so that the next sync fetches it again
    assert filled.get_message(key(2)) is not None
    assert filled.get_analysis(key(2)) is not None
    assert filled.check_consistency() == (0, 0)

    filled.remove_messages("work", "INBOX", VALIDITY, [2])
    filled.check_consistency()
    index = sqlite3.connect(filled.cache_dir / INDEX_FILE)
    assert index.execute("SELECT count(*) FROM analysis").fetchone()[0] == 0
    index.close()


def test_fts_index_stays_consistent(filled: Store) -> None:
    filled.update_flags("work", "INBOX", VALIDITY, {1: ["\\Seen", "\\Flagged"]})
    filled.remove_messages("work", "INBOX", VALIDITY, [3])
    filled.add_messages("work", "INBOX", VALIDITY, [new(2, make_message(subject="Changed"))])
    filled.remove_folder("work", "Sent")
    filled.remove_account("home")
    index = sqlite3.connect(filled.cache_dir / INDEX_FILE)
    # FTS5's own integrity check compares the index with the content table.
    index.execute("INSERT INTO messages_fts (messages_fts, rank) VALUES ('integrity-check', 1)")
    assert index.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
    index.close()
    assert sorted(m.key.uid for m in filled.search("lighthouse")) == [1]


def test_strings_with_surrogates_and_nuls_can_be_stored(store: Store) -> None:
    from triage.mime import ParsedMessage

    store.upsert_folder("work", "INBOX")
    store.reset_folder("work", "INBOX", VALIDITY)
    hostile = ParsedMessage(
        subject="bad \udcff surrogate \x00 nul",
        from_=[Address("n\udc80", "a\x00@example.org")],
        text="body \ud800 text \x00",
        message_id="<id\udcfe@example.org>",
        references=["<r\x00@example.org>"],
    )
    store.add_messages("work", "INBOX", VALIDITY, [NewMessage(1, [], T0, b"raw", hostile)])
    message = store.get_message(key(1))
    assert message is not None
    assert message.subject == "bad � surrogate  nul"
    assert message.body == "body � text "
    assert [m.key.uid for m in store.search("surrogate")] == [1]


def test_concurrent_readers_and_writer(store: Store) -> None:
    store.upsert_folder("work", "INBOX")
    store.reset_folder("work", "INBOX", VALIDITY)
    total = 150
    errors: list[BaseException] = []
    done = threading.Event()

    def write() -> None:
        try:
            for uid in range(1, total + 1):
                raw = make_message(subject=f"number {uid}", text=f"concurrent text {uid}\n")
                store.add_messages("work", "INBOX", VALIDITY, [new(uid, raw)])
                if uid % 10 == 0:
                    store.update_flags("work", "INBOX", VALIDITY, {uid: ["\\Seen"]})
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    def read() -> None:
        try:
            while not done.is_set():
                hits = store.search("concurrent", limit=500)
                for hit in hits:
                    message = store.get_message(hit.key)
                    assert message is not None
                    assert message.body.startswith("concurrent text")
                    assert store.get_raw(hit.key) is not None
                store.counts()
        except BaseException as exc:
            errors.append(exc)

    def write_analysis() -> None:
        try:
            while not done.is_set():
                for summary in store.unanalysed("v1", limit=20):
                    store.set_analysis(summary.key, "v1", {"ok": True})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write), threading.Thread(target=write_analysis)]
    threads += [threading.Thread(target=read) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert errors == []
    assert len(store.search("concurrent", limit=500)) == total
    assert store.counts("work")[0].unread == total - total // 10
    store.close()  # connections opened by other threads are closed too

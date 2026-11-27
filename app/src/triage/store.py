"""The local cache of the user's mail: SQLite databases inside the email sandbox.

There are two database files in the cache directory:

* ``index.sqlite3`` holds folders, message metadata, the plain-text bodies with a full-text
  index over them, and the results of analysing messages;
* ``raw.sqlite3`` holds the raw messages, compressed, keyed by the SHA-256 of their content.

The raw messages are kept in a database rather than as one file each because the cache directory
is a bind mount, and Docker Desktop is slow with many small files. Keying them by content means
that a message that is moved between folders, or that appears in several folders, is stored once.

The two files are not updated atomically together. A raw message is always committed before the
metadata that refers to it, and unreferenced raw messages are collected later, so that a crash
can leave garbage behind but never metadata without content.

A :class:`Store` may be used from several threads of one process. Each thread gets its own
connection; reads run concurrently (the databases are in WAL mode) and writes are serialised.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from triage.mime import Address, Attachment, ParsedMessage, clean_text, parse_message

DEFAULT_CACHE_DIR = "/data/cache"
INDEX_FILE = "index.sqlite3"
RAW_FILE = "raw.sqlite3"

PREVIEW_CHARS = 240
_SQL_VARIABLES = 500  # how many values we put into one IN (...) list

SearchOrder = Literal["relevance", "date"]


class StoreError(Exception):
    pass


class StaleFolderError(StoreError):
    """The folder is unknown to the cache, or cached under a different ``uid_validity``."""


# --------------------------------------------------------------------------------------------
# Data types


@dataclass(frozen=True, slots=True)
class MessageKey:
    """Identifies a message: UIDs are only meaningful together with the ``uid_validity``."""

    account: str
    folder: str
    uid_validity: int
    uid: int


@dataclass(frozen=True, slots=True)
class FolderState:
    account: str
    name: str
    role: str | None
    delimiter: str | None
    selectable: bool
    protected: bool
    uid_validity: int | None
    uid_next: int | None
    synced_at: datetime | None


@dataclass(frozen=True, slots=True)
class FolderCounts:
    account: str
    folder: str
    role: str | None
    total: int
    unread: int


@dataclass(frozen=True, slots=True)
class NewMessage:
    """A message to be added to the cache."""

    uid: int
    flags: Sequence[str]
    internal_date: datetime
    raw: bytes
    parsed: ParsedMessage | None = None
    """The parsed form. If it is None, the message is stored without searchable content."""
    parse_error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageSummary:
    """What lists and search results show of a message."""

    key: MessageKey
    subject: str
    from_: tuple[Address, ...]
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    timestamp: datetime
    """When the message was sent, or failing that when it arrived. Used for ordering."""
    internal_date: datetime
    date: datetime | None
    flags: tuple[str, ...]
    size: int
    message_id: str | None
    in_reply_to: str | None
    preview: str
    attachment_count: int
    snippet: str | None = None
    """For search results: the part of the message that matched."""

    @property
    def seen(self) -> bool:
        return "\\Seen" in self.flags

    @property
    def flagged(self) -> bool:
        return "\\Flagged" in self.flags

    @property
    def answered(self) -> bool:
        return "\\Answered" in self.flags


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredMessage(MessageSummary):
    """Everything the index knows about a message."""

    sender: tuple[Address, ...]
    reply_to: tuple[Address, ...]
    references: tuple[str, ...]
    body: str
    has_html: bool
    attachments: tuple[Attachment, ...]
    parse_error: str | None
    content_hash: bytes


@dataclass(frozen=True, slots=True)
class Analysis:
    analyser: str
    result: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoreStats:
    messages: int
    raw_messages: int
    raw_bytes: int
    stored_bytes: int


# --------------------------------------------------------------------------------------------
# Schema

Migration = Callable[[sqlite3.Connection], None]


# Schema changes are applied one statement at a time: sqlite3's executescript() would
# commit the transaction that makes a migration atomic.
_INDEX_V1: tuple[str, ...] = (
    """
    CREATE TABLE folders (
        id           INTEGER PRIMARY KEY,
        account      TEXT NOT NULL,
        name         TEXT NOT NULL,
        role         TEXT,
        delimiter    TEXT,
        selectable   INTEGER NOT NULL DEFAULT 1,
        protected    INTEGER NOT NULL DEFAULT 0,
        uid_validity INTEGER,
        uid_next     INTEGER,
        synced_at    INTEGER,
        UNIQUE (account, name)
    )
    """,
    """
    CREATE TABLE messages (
        id            INTEGER PRIMARY KEY,
        folder_id     INTEGER NOT NULL REFERENCES folders (id) ON DELETE CASCADE,
        uid           INTEGER NOT NULL,
        content_hash  BLOB NOT NULL,
        size          INTEGER NOT NULL,
        flags         TEXT NOT NULL,     -- JSON list
        seen          INTEGER NOT NULL,
        flagged       INTEGER NOT NULL,
        answered      INTEGER NOT NULL,
        internal_date INTEGER NOT NULL,  -- seconds since the epoch
        date          INTEGER,           -- Date header, seconds since the epoch
        timestamp     INTEGER NOT NULL,  -- what the message is ordered by
        message_id    TEXT,
        in_reply_to   TEXT,
        subject       TEXT NOT NULL,
        sender        TEXT NOT NULL,     -- From, as text, for the full-text index
        from_address  TEXT NOT NULL,     -- first From address, lower case
        recipients    TEXT NOT NULL,     -- To and Cc, as text, for the full-text index
        addresses     TEXT NOT NULL,     -- JSON object of address lists
        refs          TEXT NOT NULL,     -- JSON list, the References header
        body          TEXT NOT NULL,
        has_html      INTEGER NOT NULL,
        attachments   TEXT NOT NULL,     -- JSON list
        parse_error   TEXT,
        UNIQUE (folder_id, uid)
    )
    """,
    """
    CREATE INDEX messages_by_time ON messages (folder_id, timestamp)
    """,
    """
    CREATE INDEX messages_by_hash ON messages (content_hash)
    """,
    """
    CREATE INDEX messages_by_sender ON messages (from_address)
    """,
    """
    -- Message-IDs that a message carries or refers to, for finding conversations.
    CREATE TABLE message_refs (
        message INTEGER NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
        ref     TEXT NOT NULL,
        PRIMARY KEY (message, ref)
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX message_refs_by_ref ON message_refs (ref)
    """,
    """
    CREATE VIRTUAL TABLE messages_fts USING fts5 (
        subject, sender, recipients, body,
        content = 'messages', content_rowid = 'id',
        tokenize = 'porter unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts (rowid, subject, sender, recipients, body)
        VALUES (new.id, new.subject, new.sender, new.recipients, new.body);
    END
    """,
    """
    CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts (messages_fts, rowid, subject, sender, recipients, body)
        VALUES ('delete', old.id, old.subject, old.sender, old.recipients, old.body);
    END
    """,
    """
    CREATE TRIGGER messages_fts_update
    AFTER UPDATE OF subject, sender, recipients, body ON messages BEGIN
        INSERT INTO messages_fts (messages_fts, rowid, subject, sender, recipients, body)
        VALUES ('delete', old.id, old.subject, old.sender, old.recipients, old.body);
        INSERT INTO messages_fts (rowid, subject, sender, recipients, body)
        VALUES (new.id, new.subject, new.sender, new.recipients, new.body);
    END
    """,
    """
    -- Results of analysing a message (by the local LLM), keyed by content so that they
    -- survive the message being moved to another folder.
    CREATE TABLE analysis (
        account      TEXT NOT NULL,
        content_hash BLOB NOT NULL,
        analyser     TEXT NOT NULL,
        result       TEXT NOT NULL,     -- JSON object
        created_at   INTEGER NOT NULL,
        PRIMARY KEY (account, content_hash)
    ) WITHOUT ROWID
    """,
)

_RAW_V1: tuple[str, ...] = (
    """
    CREATE TABLE raw_messages (
        id       INTEGER PRIMARY KEY,
        hash     BLOB NOT NULL UNIQUE,   -- SHA-256 of the uncompressed message
        size     INTEGER NOT NULL,       -- uncompressed size
        encoding INTEGER NOT NULL,       -- 0: stored as is, 1: zlib
        data     BLOB NOT NULL
    )
    """,
)


def _index_v1(db: sqlite3.Connection) -> None:
    for statement in _INDEX_V1:
        db.execute(statement)


def _raw_v1(db: sqlite3.Connection) -> None:
    for statement in _RAW_V1:
        db.execute(statement)


INDEX_MIGRATIONS: tuple[Migration, ...] = (_index_v1,)
RAW_MIGRATIONS: tuple[Migration, ...] = (_raw_v1,)


def _migrate(db: sqlite3.Connection, migrations: Sequence[Migration], what: str) -> None:
    """Bring a database up to the newest schema version, one migration per transaction."""
    while True:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(migrations):
            raise StoreError(
                f"the {what} database has schema version {version}, but this version of the "
                f"app only knows versions up to {len(migrations)}"
            )
        if version == len(migrations):
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            # Another connection may have migrated while we were waiting for the lock.
            if db.execute("PRAGMA user_version").fetchone()[0] == version:
                migrations[version](db)
                db.execute(f"PRAGMA user_version = {version + 1}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


# --------------------------------------------------------------------------------------------
# Conversions


def _epoch(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())


def _from_epoch(value: int) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def _opt_from_epoch(value: int | None) -> datetime | None:
    return None if value is None else _from_epoch(value)


def _addresses_json(addresses: Iterable[Address]) -> list[list[str]]:
    return [[a.name, a.address] for a in addresses]


def _addresses(value: object) -> tuple[Address, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(Address(str(item[0]), str(item[1])) for item in value if len(item) == 2)


def _preview(body: str) -> str:
    return " ".join(body[: PREVIEW_CHARS * 4].split())[:PREVIEW_CHARS]


_SUMMARY_COLUMNS = (
    "m.id, f.account, f.name, f.uid_validity, m.uid, m.subject, m.addresses, m.timestamp, "
    "m.internal_date, m.date, m.flags, m.size, m.message_id, m.in_reply_to, "
    f"substr(m.body, 1, {PREVIEW_CHARS * 4}), json_array_length(m.attachments)"
)


def _summary(row: Sequence[Any], snippet: str | None = None) -> MessageSummary:
    addresses = json.loads(row[6])
    return MessageSummary(
        key=MessageKey(row[1], row[2], row[3], row[4]),
        subject=row[5],
        from_=_addresses(addresses.get("from")),
        to=_addresses(addresses.get("to")),
        cc=_addresses(addresses.get("cc")),
        timestamp=_from_epoch(row[7]),
        internal_date=_from_epoch(row[8]),
        date=_opt_from_epoch(row[9]),
        flags=tuple(json.loads(row[10])),
        size=row[11],
        message_id=row[12],
        in_reply_to=row[13],
        preview=_preview(row[14]),
        attachment_count=row[15],
        snippet=snippet,
    )


_FTS_COLUMNS = {
    "subject": "subject",
    "from": "sender",
    "sender": "sender",
    "to": "recipients",
    "cc": "recipients",
    "recipients": "recipients",
    "body": "body",
}
_QUERY_TOKEN = re.compile(r'(?:([A-Za-z]+):)?(?:"([^"]*)"|(\S+))')


def fts_query(text: str) -> str | None:
    """Translate what a person or a model types into a safe FTS5 query.

    Words must all match. ``"double quotes"`` make a phrase, a trailing ``*`` matches a prefix,
    and ``subject:``, ``from:``, ``to:``, and ``body:`` restrict a term to that part of the
    message. Everything else is taken literally, so no input can cause a syntax error. Returns
    None if the text contains nothing to search for.
    """
    terms = []
    for match in _QUERY_TOKEN.finditer(clean_text(text[:2000])):
        prefix, phrase, word = match.group(1), match.group(2), match.group(3)
        column = _FTS_COLUMNS.get(prefix.lower()) if prefix else None
        if prefix and column is None:
            # Not one of our prefixes, so the colon was part of the word (e.g. a URL).
            term = f"{prefix}:{phrase if phrase is not None else word}"
            is_prefix = False
        else:
            term = phrase if phrase is not None else word
            is_prefix = phrase is None and term.endswith("*")
        term = term.rstrip("*") if is_prefix else term
        term = " ".join(term.replace('"', " ").split())
        if not any(character.isalnum() for character in term):
            continue
        quoted = f'"{term}"'
        if is_prefix:
            quoted += "*"
        terms.append(f"{column} : {quoted}" if column else quoted)
        if len(terms) >= 32:
            break
    return " AND ".join(terms) if terms else None


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _chunks[T](values: Sequence[T], size: int = _SQL_VARIABLES) -> Iterator[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


# --------------------------------------------------------------------------------------------
# The store


class Store:
    def __init__(self, cache_dir: str | os.PathLike[str] | None = None):
        if cache_dir is None:
            cache_dir = os.environ.get("TRIAGE_CACHE_DIR", DEFAULT_CACHE_DIR)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.cache_dir / INDEX_FILE
        self._raw_path = self.cache_dir / RAW_FILE
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._closed = False
        _migrate(self._index(), INDEX_MIGRATIONS, "index")
        _migrate(self._raw(), RAW_MIGRATIONS, "raw message")

    # -- connections ---------------------------------------------------------------------

    def _open(self, path: Path) -> sqlite3.Connection:
        if self._closed:
            raise StoreError("the store is closed")
        # Autocommit mode: transactions are opened explicitly where they are needed. Each
        # connection is used by one thread only; check_same_thread is off just so that
        # close() can be called from another thread.
        db = sqlite3.connect(path, timeout=60.0, isolation_level=None, check_same_thread=False)
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 60000")
        with self._connections_lock:
            self._connections.append(db)
        return db

    def _index(self) -> sqlite3.Connection:
        db: sqlite3.Connection | None = getattr(self._local, "index", None)
        if db is None:
            db = self._open(self._index_path)
            self._local.index = db
        return db

    def _raw(self) -> sqlite3.Connection:
        db: sqlite3.Connection | None = getattr(self._local, "raw", None)
        if db is None:
            db = self._open(self._raw_path)
            self._local.raw = db
        return db

    def close(self) -> None:
        """Close all connections. The store cannot be used afterwards."""
        with self._write_lock, self._connections_lock:
            self._closed = True
            for db in self._connections:
                with contextlib.suppress(sqlite3.Error):
                    db.close()
            self._connections.clear()
        self._local = threading.local()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextlib.contextmanager
    def _write(self, db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """A write transaction. Nested use joins the transaction that is already open."""
        with self._write_lock:
            if db.in_transaction:
                yield db
                return
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # -- folders -------------------------------------------------------------------------

    @staticmethod
    def _folder_state(row: Sequence[Any]) -> FolderState:
        return FolderState(
            account=row[0],
            name=row[1],
            role=row[2],
            delimiter=row[3],
            selectable=bool(row[4]),
            protected=bool(row[5]),
            uid_validity=row[6],
            uid_next=row[7],
            synced_at=_opt_from_epoch(row[8]),
        )

    _FOLDER_COLUMNS = (
        "account, name, role, delimiter, selectable, protected, uid_validity, uid_next, synced_at"
    )

    def upsert_folder(
        self,
        account: str,
        name: str,
        *,
        role: str | None = None,
        delimiter: str | None = None,
        selectable: bool = True,
        protected: bool = False,
    ) -> None:
        """Record that a folder exists, or update what is known about it."""
        with self._write(self._index()) as db:
            db.execute(
                """
                INSERT INTO folders (account, name, role, delimiter, selectable, protected)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (account, name) DO UPDATE SET
                    role = excluded.role, delimiter = excluded.delimiter,
                    selectable = excluded.selectable, protected = excluded.protected
                """,
                (account, name, role, delimiter, int(selectable), int(protected)),
            )

    def folder(self, account: str, name: str) -> FolderState | None:
        row = (
            self._index()
            .execute(
                f"SELECT {self._FOLDER_COLUMNS} FROM folders WHERE account = ? AND name = ?",  # noqa: S608
                (account, name),
            )
            .fetchone()
        )
        return None if row is None else self._folder_state(row)

    def folders(self, account: str | None = None) -> list[FolderState]:
        where, parameters = ("WHERE account = ?", (account,)) if account is not None else ("", ())
        rows = self._index().execute(
            f"SELECT {self._FOLDER_COLUMNS} FROM folders {where} ORDER BY account, name",  # noqa: S608
            parameters,
        )
        return [self._folder_state(row) for row in rows]

    def accounts(self) -> list[str]:
        rows = self._index().execute("SELECT DISTINCT account FROM folders ORDER BY account")
        return [row[0] for row in rows]

    def _folder_id(
        self, db: sqlite3.Connection, account: str, folder: str, uid_validity: int | None
    ) -> int:
        row = db.execute(
            "SELECT id, uid_validity FROM folders WHERE account = ? AND name = ?",
            (account, folder),
        ).fetchone()
        if row is None:
            raise StaleFolderError(f"folder {folder!r} of account {account!r} is not cached")
        if uid_validity is not None and row[1] != uid_validity:
            raise StaleFolderError(
                f"folder {folder!r} is cached with uid_validity {row[1]}, not {uid_validity}"
            )
        return int(row[0])

    def reset_folder(self, account: str, folder: str, uid_validity: int) -> None:
        """Drop everything cached for the folder and start over under a new ``uid_validity``."""
        with self._write(self._index()) as db:
            folder_id = self._folder_id(db, account, folder, None)
            db.execute("DELETE FROM messages WHERE folder_id = ?", (folder_id,))
            db.execute(
                "UPDATE folders SET uid_validity = ?, uid_next = NULL, synced_at = NULL "
                "WHERE id = ?",
                (uid_validity, folder_id),
            )

    def mark_folder_synced(
        self,
        account: str,
        folder: str,
        uid_validity: int,
        *,
        uid_next: int | None = None,
        when: datetime | None = None,
    ) -> None:
        with self._write(self._index()) as db:
            folder_id = self._folder_id(db, account, folder, uid_validity)
            db.execute(
                "UPDATE folders SET uid_next = ?, synced_at = ? WHERE id = ?",
                (uid_next, _epoch(when or datetime.now(UTC)), folder_id),
            )

    def remove_folder(self, account: str, folder: str) -> None:
        with self._write(self._index()) as db:
            row = db.execute(
                "SELECT id FROM folders WHERE account = ? AND name = ?", (account, folder)
            ).fetchone()
            if row is not None:
                db.execute("DELETE FROM messages WHERE folder_id = ?", (row[0],))
                db.execute("DELETE FROM folders WHERE id = ?", (row[0],))

    def remove_account(self, account: str) -> None:
        """Forget everything about an account, including its analysis results."""
        with self._write(self._index()) as db:
            db.execute(
                "DELETE FROM messages WHERE folder_id IN "
                "(SELECT id FROM folders WHERE account = ?)",
                (account,),
            )
            db.execute("DELETE FROM folders WHERE account = ?", (account,))
            db.execute("DELETE FROM analysis WHERE account = ?", (account,))

    # -- writing messages ----------------------------------------------------------------

    def _put_raw(self, raw: bytes) -> bytes:
        digest = hashlib.sha256(raw).digest()
        db = self._raw()
        if db.execute("SELECT 1 FROM raw_messages WHERE hash = ?", (digest,)).fetchone() is None:
            compressed = zlib.compress(raw, 6)
            encoding, data = (1, compressed) if len(compressed) < len(raw) else (0, raw)
            with self._write(db):
                db.execute(
                    "INSERT OR IGNORE INTO raw_messages (hash, size, encoding, data) "
                    "VALUES (?, ?, ?, ?)",
                    (digest, len(raw), encoding, data),
                )
        return digest

    def add_messages(
        self,
        account: str,
        folder: str,
        uid_validity: int,
        messages: Iterable[NewMessage],
    ) -> int:
        """Add messages to a folder, replacing any that are cached under the same UID.

        The folder must be cached under ``uid_validity`` (see :meth:`reset_folder`); otherwise
        :class:`StaleFolderError` is raised. Returns the number of messages added. All of them
        are added in one transaction.
        """
        count = 0
        with self._write_lock, self._write(self._index()) as db:
            folder_id = self._folder_id(db, account, folder, uid_validity)
            for message in messages:
                # The raw message is committed (in its own database) before the metadata.
                digest = self._put_raw(message.raw)
                self._insert(db, folder_id, message, digest)
                count += 1
        return count

    @staticmethod
    def _insert(db: sqlite3.Connection, folder_id: int, message: NewMessage, digest: bytes) -> None:
        parsed = message.parsed or ParsedMessage()
        flags = sorted(set(message.flags))
        internal_date = _epoch(message.internal_date)
        date = _epoch(parsed.date) if parsed.date is not None else None
        # The Date header is under the sender's control. Use it for ordering unless it claims
        # that the message was sent after it arrived.
        timestamp = date if date is not None and date <= internal_date + 86400 else internal_date
        addresses = {
            "from": _addresses_json(parsed.from_),
            "sender": _addresses_json(parsed.sender),
            "to": _addresses_json(parsed.to),
            "cc": _addresses_json(parsed.cc),
            "reply_to": _addresses_json(parsed.reply_to),
        }
        attachments = [
            [a.filename, a.content_type, a.size, a.content_id, a.inline] for a in parsed.attachments
        ]
        from_address = parsed.from_[0].address.lower() if parsed.from_ else ""
        db.execute("DELETE FROM messages WHERE folder_id = ? AND uid = ?", (folder_id, message.uid))
        cursor = db.execute(
            """
            INSERT INTO messages (
                folder_id, uid, content_hash, size, flags, seen, flagged, answered,
                internal_date, date, timestamp, message_id, in_reply_to, subject, sender,
                from_address, recipients, addresses, refs, body, has_html, attachments,
                parse_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                folder_id,
                message.uid,
                digest,
                len(message.raw),
                json.dumps(flags),
                int("\\Seen" in flags),
                int("\\Flagged" in flags),
                int("\\Answered" in flags),
                internal_date,
                date,
                timestamp,
                clean_text(parsed.message_id) if parsed.message_id else None,
                clean_text(parsed.in_reply_to) if parsed.in_reply_to else None,
                clean_text(parsed.subject),
                clean_text(", ".join(str(a) for a in parsed.from_)),
                clean_text(from_address),
                clean_text(", ".join(str(a) for a in (*parsed.to, *parsed.cc))),
                clean_text(json.dumps(addresses)),
                clean_text(json.dumps(parsed.references)),
                clean_text(parsed.text),
                int(parsed.html is not None),
                clean_text(json.dumps(attachments)),
                message.parse_error,
            ),
        )
        refs = {parsed.message_id, parsed.in_reply_to, *parsed.references} - {None}
        db.executemany(
            "INSERT OR IGNORE INTO message_refs (message, ref) VALUES (?, ?)",
            [(cursor.lastrowid, clean_text(ref)) for ref in refs if ref],
        )

    def cached_flags(self, account: str, folder: str) -> dict[int, tuple[str, ...]]:
        """The UIDs cached for a folder, with their flags."""
        rows = self._index().execute(
            "SELECT m.uid, m.flags FROM messages m JOIN folders f ON f.id = m.folder_id "
            "WHERE f.account = ? AND f.name = ?",
            (account, folder),
        )
        return {row[0]: tuple(json.loads(row[1])) for row in rows}

    def update_flags(
        self, account: str, folder: str, uid_validity: int, flags: Mapping[int, Iterable[str]]
    ) -> None:
        with self._write(self._index()) as db:
            folder_id = self._folder_id(db, account, folder, uid_validity)
            for uid, values in flags.items():
                ordered = sorted(set(values))
                db.execute(
                    "UPDATE messages SET flags = ?, seen = ?, flagged = ?, answered = ? "
                    "WHERE folder_id = ? AND uid = ?",
                    (
                        json.dumps(ordered),
                        int("\\Seen" in ordered),
                        int("\\Flagged" in ordered),
                        int("\\Answered" in ordered),
                        folder_id,
                        uid,
                    ),
                )

    def remove_messages(
        self, account: str, folder: str, uid_validity: int, uids: Iterable[int]
    ) -> None:
        with self._write(self._index()) as db:
            folder_id = self._folder_id(db, account, folder, uid_validity)
            db.executemany(
                "DELETE FROM messages WHERE folder_id = ? AND uid = ?",
                [(folder_id, uid) for uid in uids],
            )

    # -- reading messages ----------------------------------------------------------------

    def _row_id(self, key: MessageKey) -> int | None:
        row = (
            self._index()
            .execute(
                "SELECT m.id FROM messages m JOIN folders f ON f.id = m.folder_id "
                "WHERE f.account = ? AND f.name = ? AND f.uid_validity = ? AND m.uid = ?",
                (key.account, key.folder, key.uid_validity, key.uid),
            )
            .fetchone()
        )
        return None if row is None else int(row[0])

    def get_message(self, key: MessageKey) -> StoredMessage | None:
        """Everything the index holds about one message, or None if it is not cached."""
        row = (
            self._index()
            .execute(
                f"SELECT {_SUMMARY_COLUMNS}, m.refs, m.body, m.has_html, m.attachments, "  # noqa: S608
                "m.parse_error, m.content_hash "
                "FROM messages m JOIN folders f ON f.id = m.folder_id "
                "WHERE f.account = ? AND f.name = ? AND f.uid_validity = ? AND m.uid = ?",
                (key.account, key.folder, key.uid_validity, key.uid),
            )
            .fetchone()
        )
        if row is None:
            return None
        summary = _summary(row)
        addresses = json.loads(row[6])
        return StoredMessage(
            key=summary.key,
            subject=summary.subject,
            from_=summary.from_,
            to=summary.to,
            cc=summary.cc,
            timestamp=summary.timestamp,
            internal_date=summary.internal_date,
            date=summary.date,
            flags=summary.flags,
            size=summary.size,
            message_id=summary.message_id,
            in_reply_to=summary.in_reply_to,
            preview=summary.preview,
            attachment_count=summary.attachment_count,
            sender=_addresses(addresses.get("sender")),
            reply_to=_addresses(addresses.get("reply_to")),
            references=tuple(json.loads(row[16])),
            body=row[17],
            has_html=bool(row[18]),
            attachments=tuple(
                Attachment(item[0], item[1], item[2], item[3], bool(item[4]))
                for item in json.loads(row[19])
            ),
            parse_error=row[20],
            content_hash=bytes(row[21]),
        )

    def get_raw(self, key: MessageKey) -> bytes | None:
        """The raw message exactly as the server delivered it."""
        row = (
            self._index()
            .execute(
                "SELECT m.content_hash FROM messages m JOIN folders f ON f.id = m.folder_id "
                "WHERE f.account = ? AND f.name = ? AND f.uid_validity = ? AND m.uid = ?",
                (key.account, key.folder, key.uid_validity, key.uid),
            )
            .fetchone()
        )
        if row is None:
            return None
        raw = (
            self._raw()
            .execute("SELECT encoding, data FROM raw_messages WHERE hash = ?", (row[0],))
            .fetchone()
        )
        if raw is None:
            return None
        return zlib.decompress(raw[1]) if raw[0] == 1 else bytes(raw[1])

    def get_parsed(self, key: MessageKey) -> ParsedMessage | None:
        """The message parsed afresh from its raw form, including the sanitised HTML."""
        raw = self.get_raw(key)
        return None if raw is None else parse_message(raw)

    def list_folder(
        self,
        account: str,
        folder: str,
        *,
        limit: int = 50,
        offset: int = 0,
        unread: bool | None = None,
        newest_first: bool = True,
    ) -> list[MessageSummary]:
        """Messages of a folder ordered by time."""
        conditions = ["f.account = ?", "f.name = ?"]
        parameters: list[Any] = [account, folder]
        if unread is not None:
            conditions.append("m.seen = ?")
            parameters.append(int(not unread))
        direction = "DESC" if newest_first else "ASC"
        rows = self._index().execute(
            f"SELECT {_SUMMARY_COLUMNS} FROM messages m JOIN folders f ON f.id = m.folder_id "  # noqa: S608
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY m.timestamp {direction}, m.uid {direction} LIMIT ? OFFSET ?",
            (*parameters, max(0, limit), max(0, offset)),
        )
        return [_summary(row) for row in rows]

    def search(
        self,
        query: str = "",
        *,
        account: str | None = None,
        folder: str | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
        unread: bool | None = None,
        flagged: bool | None = None,
        sender: str | None = None,
        has_attachments: bool | None = None,
        order: SearchOrder = "relevance",
        limit: int = 20,
        offset: int = 0,
    ) -> list[MessageSummary]:
        """Full-text search over the cached mail, optionally narrowed by filters.

        See :func:`fts_query` for the query syntax. With an empty query, the filters alone
        select the messages, newest first. ``sender`` matches a substring of the From header,
        and the time range is ``since <= timestamp < before``.
        """
        conditions: list[str] = []
        parameters: list[Any] = []
        match = fts_query(query)
        if query.strip() and match is None:
            return []  # the query consists of nothing searchable
        if account is not None:
            conditions.append("f.account = ?")
            parameters.append(account)
        if folder is not None:
            conditions.append("f.name = ?")
            parameters.append(folder)
        if since is not None:
            conditions.append("m.timestamp >= ?")
            parameters.append(_epoch(since))
        if before is not None:
            conditions.append("m.timestamp < ?")
            parameters.append(_epoch(before))
        if unread is not None:
            conditions.append("m.seen = ?")
            parameters.append(int(not unread))
        if flagged is not None:
            conditions.append("m.flagged = ?")
            parameters.append(int(flagged))
        if sender:
            conditions.append("m.sender LIKE ? ESCAPE '\\'")
            parameters.append(_like(sender))
        if has_attachments is not None:
            comparison = ">" if has_attachments else "="
            conditions.append(f"json_array_length(m.attachments) {comparison} 0")
        limits = (max(0, limit), max(0, offset))

        if match is None:
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = self._index().execute(
                f"SELECT {_SUMMARY_COLUMNS} FROM messages m "  # noqa: S608
                f"JOIN folders f ON f.id = m.folder_id {where} "
                "ORDER BY m.timestamp DESC, m.id DESC LIMIT ? OFFSET ?",
                (*parameters, *limits),
            )
            return [_summary(row) for row in rows]

        ordering = (
            "bm25(messages_fts, 6.0, 4.0, 2.0, 1.0), m.timestamp DESC"
            if order == "relevance"
            else "m.timestamp DESC, m.id DESC"
        )
        where = " AND ".join(["messages_fts MATCH ?", *conditions])
        rows = self._index().execute(
            f"SELECT {_SUMMARY_COLUMNS}, "  # noqa: S608
            "snippet(messages_fts, -1, '', '', ' … ', 24) "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            f"JOIN folders f ON f.id = m.folder_id WHERE {where} "
            f"ORDER BY {ordering} LIMIT ? OFFSET ?",
            (match, *parameters, *limits),
        )
        return [_summary(row, " ".join(str(row[16]).split())) for row in rows]

    def conversation(self, key: MessageKey, *, limit: int = 200) -> list[MessageSummary]:
        """The messages of the same account that belong to the same conversation, oldest first.

        Two messages belong together if one refers to the other, or to a message that the
        other also refers to (through the Message-ID, In-Reply-To, and References headers),
        followed transitively. The message itself is always included.
        """
        db = self._index()
        start = self._row_id(key)
        if start is None:
            return []
        members = {start}
        known_refs: set[str] = set()
        frontier = [start]
        # Bounded breadth-first search over the "shares a Message-ID" relation.
        for _round in range(16):
            new_refs: list[str] = []
            for chunk in _chunks(frontier):
                marks = ", ".join("?" * len(chunk))
                rows = db.execute(
                    f"SELECT DISTINCT ref FROM message_refs WHERE message IN ({marks})",  # noqa: S608
                    tuple(chunk),
                )
                for (ref,) in rows:
                    if ref not in known_refs:
                        known_refs.add(ref)
                        new_refs.append(ref)
            frontier = []
            for chunk_refs in _chunks(new_refs):
                marks = ", ".join("?" * len(chunk_refs))
                rows = db.execute(
                    "SELECT DISTINCT r.message FROM message_refs r "  # noqa: S608
                    "JOIN messages m ON m.id = r.message JOIN folders f ON f.id = m.folder_id "
                    f"WHERE f.account = ? AND r.ref IN ({marks})",
                    (key.account, *chunk_refs),
                )
                for (message,) in rows:
                    if message not in members:
                        members.add(message)
                        frontier.append(message)
            if not frontier or len(members) >= limit * 4:
                break

        summaries: list[MessageSummary] = []
        for chunk in _chunks(sorted(members)):
            marks = ", ".join("?" * len(chunk))
            rows = db.execute(
                f"SELECT {_SUMMARY_COLUMNS} FROM messages m "  # noqa: S608
                f"JOIN folders f ON f.id = m.folder_id WHERE m.id IN ({marks})",
                tuple(chunk),
            )
            summaries.extend(_summary(row) for row in rows)
        summaries.sort(key=lambda s: (s.timestamp, s.key.folder, s.key.uid))
        return summaries[:limit]

    def counts(self, account: str | None = None) -> list[FolderCounts]:
        """Total and unread message counts of every cached folder."""
        where, parameters = ("WHERE f.account = ?", (account,)) if account is not None else ("", ())
        rows = self._index().execute(
            "SELECT f.account, f.name, f.role, count(m.id), "  # noqa: S608
            "coalesce(sum(m.seen = 0), 0) "
            f"FROM folders f LEFT JOIN messages m ON m.folder_id = f.id {where} "
            "GROUP BY f.id ORDER BY f.account, f.name",
            parameters,
        )
        return [FolderCounts(row[0], row[1], row[2], row[3], row[4]) for row in rows]

    def stats(self) -> StoreStats:
        messages = self._index().execute("SELECT count(*) FROM messages").fetchone()[0]
        raw = (
            self._raw()
            .execute(
                "SELECT count(*), coalesce(sum(size), 0), coalesce(sum(length(data)), 0) "
                "FROM raw_messages"
            )
            .fetchone()
        )
        return StoreStats(messages, raw[0], raw[1], raw[2])

    # -- analysis ------------------------------------------------------------------------

    def set_analysis(self, key: MessageKey, analyser: str, result: Mapping[str, Any]) -> bool:
        """Record the result of analysing a message. Returns False if it is no longer cached.

        ``analyser`` names what produced the result (e.g. a model and prompt version), so that
        messages can be found again for re-analysis when it changes.
        """
        with self._write(self._index()) as db:
            row = db.execute(
                "SELECT m.content_hash FROM messages m JOIN folders f ON f.id = m.folder_id "
                "WHERE f.account = ? AND f.name = ? AND f.uid_validity = ? AND m.uid = ?",
                (key.account, key.folder, key.uid_validity, key.uid),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                "INSERT OR REPLACE INTO analysis "
                "(account, content_hash, analyser, result, created_at) VALUES (?, ?, ?, ?, ?)",
                (key.account, row[0], analyser, clean_text(json.dumps(result)), int(time.time())),
            )
            return True

    def get_analysis(self, key: MessageKey) -> Analysis | None:
        row = (
            self._index()
            .execute(
                "SELECT a.analyser, a.result, a.created_at FROM analysis a "
                "JOIN messages m ON m.content_hash = a.content_hash "
                "JOIN folders f ON f.id = m.folder_id AND f.account = a.account "
                "WHERE f.account = ? AND f.name = ? AND f.uid_validity = ? AND m.uid = ?",
                (key.account, key.folder, key.uid_validity, key.uid),
            )
            .fetchone()
        )
        if row is None:
            return None
        return Analysis(row[0], json.loads(row[1]), _from_epoch(row[2]))

    def unanalysed(
        self,
        analyser: str,
        *,
        account: str | None = None,
        folder: str | None = None,
        limit: int = 50,
    ) -> list[MessageSummary]:
        """Messages without a result from this analyser, newest first.

        A message that is cached in several folders is returned once.
        """
        conditions = ["(a.content_hash IS NULL OR a.analyser != ?)"]
        parameters: list[Any] = [analyser]
        if account is not None:
            conditions.append("f.account = ?")
            parameters.append(account)
        if folder is not None:
            conditions.append("f.name = ?")
            parameters.append(folder)
        rows = self._index().execute(
            f"SELECT {_SUMMARY_COLUMNS}, max(m.timestamp) AS newest FROM messages m "  # noqa: S608
            "JOIN folders f ON f.id = m.folder_id "
            "LEFT JOIN analysis a "
            "ON a.account = f.account AND a.content_hash = m.content_hash "
            f"WHERE {' AND '.join(conditions)} "
            "GROUP BY f.account, m.content_hash ORDER BY newest DESC, m.id DESC LIMIT ?",
            (*parameters, max(0, limit)),
        )
        return [_summary(row) for row in rows]

    # -- housekeeping --------------------------------------------------------------------

    def check_consistency(self) -> tuple[int, int]:
        """Repair what a crash may have left behind.

        Removes metadata whose raw message is missing (so that the next sync fetches the
        message again), raw messages that nothing refers to, and analysis results of messages
        that are gone. Returns the number of metadata rows and raw messages removed.
        """
        with self._write_lock:
            index, raw = self._index(), self._raw()
            wanted = {bytes(row[0]) for row in index.execute("SELECT content_hash FROM messages")}
            present = {bytes(row[0]) for row in raw.execute("SELECT hash FROM raw_messages")}
            missing = sorted(wanted - present)
            garbage = sorted(present - wanted)
            removed = 0
            if missing:
                with self._write(index):
                    for digest in missing:
                        removed += index.execute(
                            "DELETE FROM messages WHERE content_hash = ?", (digest,)
                        ).rowcount
            if garbage:
                with self._write(raw):
                    raw.executemany(
                        "DELETE FROM raw_messages WHERE hash = ?", [(h,) for h in garbage]
                    )
            with self._write(index):
                index.execute(
                    "DELETE FROM analysis WHERE NOT EXISTS ("
                    "SELECT 1 FROM messages m JOIN folders f ON f.id = m.folder_id "
                    "WHERE f.account = analysis.account "
                    "AND m.content_hash = analysis.content_hash)"
                )
            return removed, len(garbage)

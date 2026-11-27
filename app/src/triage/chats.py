"""Conversations between the user and the assistant, kept in the cache directory.

A conversation is a list of items: what the user wrote, what the assistant answered, and what
the assistant did in between (tool items). Items also carry what the user interface shows next
to them: message cards, a change request, or a proposed change to the policy.

Messages are referred to by short handles such as ``m3``, which are easier for a small model
to repeat correctly than long identifiers. A handle belongs to one conversation.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from triage.store import DEFAULT_CACHE_DIR, MessageKey, MessageSummary

Role = Literal["user", "assistant", "tool", "notice"]

CHAT_FILE = "chat.sqlite3"
MAX_TITLE = 80


def encode_key(key: MessageKey) -> str:
    """A URL-safe string that identifies a message."""
    data = json.dumps([key.account, key.folder, key.uid_validity, key.uid]).encode()
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_key(text: str) -> MessageKey | None:
    try:
        padded = text + "=" * (-len(text) % 4)
        account, folder, uid_validity, uid = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    if not (isinstance(account, str) and isinstance(folder, str)):
        return None
    if not (type(uid_validity) is int and type(uid) is int):
        return None
    return MessageKey(account, folder, uid_validity, uid)


def message_card(message: MessageSummary, handle: str = "") -> dict[str, Any]:
    """What the user interface needs in order to show a message in a list."""
    return {
        "key": encode_key(message.key),
        "handle": handle,
        "account": message.key.account,
        "folder": message.key.folder,
        "subject": message.subject,
        "from": [{"name": a.name, "address": a.address} for a in message.from_],
        "date": message.timestamp.isoformat(),
        "preview": message.snippet or message.preview,
        "unread": not message.seen,
        "flagged": message.flagged,
        "has_attachments": message.attachment_count > 0,
    }


@dataclass(frozen=True, slots=True)
class Conversation:
    id: int
    title: str
    updated: datetime

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "updated": self.updated.isoformat()}


@dataclass(frozen=True, slots=True)
class Item:
    id: int
    conversation_id: int
    role: Role
    time: datetime
    text: str
    tool: Mapping[str, Any] | None
    messages: Sequence[Mapping[str, Any]]
    change_request: Mapping[str, Any] | None
    policy_proposal: Mapping[str, Any] | None
    llm: Mapping[str, Any] | None
    """What is needed to show this item to the model again; not sent to the interface."""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "time": self.time.isoformat(),
            "text": self.text,
            "tool": self.tool,
            "messages": list(self.messages),
            "change_request": self.change_request,
            "policy_proposal": self.policy_proposal,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    time INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_by_conversation ON items(conversation_id, id);
CREATE TABLE IF NOT EXISTS handles (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (conversation_id, number),
    UNIQUE (conversation_id, key)
);
CREATE TABLE IF NOT EXISTS undone (seq INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS change_requests (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    time INTEGER NOT NULL,
    state TEXT NOT NULL
);
"""


def _epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC)


class ChatStore:
    def __init__(self, cache_dir: str | os.PathLike[str] | None = None):
        if cache_dir is None:
            cache_dir = os.environ.get("TRIAGE_CACHE_DIR", DEFAULT_CACHE_DIR)
        directory = Path(cache_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            directory / CHAT_FILE, timeout=60.0, isolation_level=None, check_same_thread=False
        )
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- conversations -------------------------------------------------------------------

    def create(self, title: str = "New conversation") -> Conversation:
        now = datetime.now(UTC)
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO conversations (title, created, updated) VALUES (?, ?, ?)",
                (title[:MAX_TITLE], _epoch_ms(now), _epoch_ms(now)),
            )
            return Conversation(int(cursor.lastrowid or 0), title[:MAX_TITLE], now)

    def conversations(self, limit: int = 200) -> list[Conversation]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, title, updated FROM conversations "
                "ORDER BY updated DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Conversation(row[0], row[1], _from_epoch_ms(row[2])) for row in rows]

    def conversation(self, conversation_id: int) -> Conversation | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, title, updated FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return Conversation(row[0], row[1], _from_epoch_ms(row[2])) if row else None

    def find_or_create(self, title: str) -> Conversation:
        """The oldest conversation with this title, created if there is none."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, title, updated FROM conversations WHERE title = ? ORDER BY id LIMIT 1",
                (title[:MAX_TITLE],),
            ).fetchone()
            if row is None:
                return self.create(title)
        return Conversation(row[0], row[1], _from_epoch_ms(row[2]))

    def set_title(self, conversation_id: int, title: str) -> None:
        title = " ".join(title.split())[:MAX_TITLE] or "Conversation"
        with self._lock:
            self._db.execute(
                "UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)
            )

    # -- items ---------------------------------------------------------------------------

    def _item(self, row: Sequence[Any]) -> Item:
        payload = json.loads(row[4])
        return Item(
            id=row[0],
            conversation_id=row[1],
            role=row[2],
            time=_from_epoch_ms(row[3]),
            text=payload.get("text", ""),
            tool=payload.get("tool"),
            messages=payload.get("messages", []),
            change_request=payload.get("change_request"),
            policy_proposal=payload.get("policy_proposal"),
            llm=payload.get("llm"),
        )

    def add_item(
        self,
        conversation_id: int,
        role: Role,
        text: str = "",
        *,
        tool: Mapping[str, Any] | None = None,
        messages: Sequence[Mapping[str, Any]] = (),
        change_request: Mapping[str, Any] | None = None,
        policy_proposal: Mapping[str, Any] | None = None,
        llm: Mapping[str, Any] | None = None,
    ) -> Item:
        now = datetime.now(UTC)
        payload = {
            "text": text,
            "tool": tool,
            "messages": list(messages),
            "change_request": change_request,
            "policy_proposal": policy_proposal,
            "llm": llm,
        }
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO items (conversation_id, role, time, payload) VALUES (?, ?, ?, ?)",
                (conversation_id, role, _epoch_ms(now), json.dumps(payload)),
            )
            self._db.execute(
                "UPDATE conversations SET updated = ? WHERE id = ?",
                (_epoch_ms(now), conversation_id),
            )
            row = self._db.execute(
                "SELECT id, conversation_id, role, time, payload FROM items WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._item(row)

    def items(self, conversation_id: int) -> list[Item]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, conversation_id, role, time, payload FROM items "
                "WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        return [self._item(row) for row in rows]

    def delete_items_from(self, conversation_id: int, first_item_id: int) -> None:
        """Remove the items of a conversation from the given one on."""
        with self._lock:
            self._db.execute(
                "DELETE FROM items WHERE conversation_id = ? AND id >= ?",
                (conversation_id, first_item_id),
            )

    def update_attachments(
        self,
        item_id: int,
        *,
        change_request: Mapping[str, Any] | None = None,
        policy_proposal: Mapping[str, Any] | None = None,
    ) -> None:
        """Replace the change request or policy proposal shown with an item (its state changes)."""
        with self._lock:
            row = self._db.execute("SELECT payload FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                return
            payload = json.loads(row[0])
            if change_request is not None:
                payload["change_request"] = change_request
            if policy_proposal is not None:
                payload["policy_proposal"] = policy_proposal
            self._db.execute(
                "UPDATE items SET payload = ? WHERE id = ?", (json.dumps(payload), item_id)
            )

    # -- handles -------------------------------------------------------------------------

    def handle_for(self, conversation_id: int, key: MessageKey) -> str:
        """The handle of a message in a conversation, allocating one if it has none yet."""
        encoded = encode_key(key)
        with self._lock:
            row = self._db.execute(
                "SELECT number FROM handles WHERE conversation_id = ? AND key = ?",
                (conversation_id, encoded),
            ).fetchone()
            if row is None:
                number = self._db.execute(
                    "SELECT COALESCE(MAX(number), 0) + 1 FROM handles WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()[0]
                self._db.execute(
                    "INSERT INTO handles (conversation_id, number, key) VALUES (?, ?, ?)",
                    (conversation_id, number, encoded),
                )
            else:
                number = row[0]
        return f"m{number}"

    def key_for(self, conversation_id: int, handle: str) -> MessageKey | None:
        handle = handle.strip().strip("[]").lower()
        if not (handle.startswith("m") and handle[1:].isdigit()):
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT key FROM handles WHERE conversation_id = ? AND number = ?",
                (conversation_id, int(handle[1:])),
            ).fetchone()
        return decode_key(row[0]) if row else None

    # -- undo ----------------------------------------------------------------------------

    def mark_undone(self, seq: int) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO undone (seq) VALUES (?)", (seq,))

    def undone(self) -> set[int]:
        with self._lock:
            return {row[0] for row in self._db.execute("SELECT seq FROM undone")}

    # -- change requests -----------------------------------------------------------------

    def record_change_request(self, request_id: str, kind: str, text: str, state: str) -> None:
        """Remember a message that the app submitted for the coding agent."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO change_requests (id, kind, text, time, state) "
                "VALUES (?, ?, ?, ?, ?)",
                (request_id, kind, text, _epoch_ms(datetime.now(UTC)), state),
            )

    def set_change_request_state(self, request_id: str, state: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE change_requests SET state = ? WHERE id = ?", (state, request_id)
            )

    def change_requests(self, limit: int = 50) -> list[dict[str, Any]]:
        """The most recent change requests, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, kind, text, time, state FROM change_requests "
                "ORDER BY time DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "kind": row[1],
                "text": row[2],
                "time": _from_epoch_ms(row[3]).isoformat(),
                "state": row[4],
            }
            for row in reversed(rows)
        ]

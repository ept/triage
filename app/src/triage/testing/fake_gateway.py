"""An in-memory stand-in for the gateway's sandbox API.

It implements the contract in ``docs/gateway-api.md`` closely enough to test the app, and to
develop the user interface, without a mail server and without the real gateway. It can be used
in-process (:meth:`FakeGateway.transport` gives an ``httpx`` transport) or as a small HTTP server
(:func:`serve`, or ``python -m triage.testing.fake_gateway`` for one filled with demo data).

The fake enforces the gateway's rules (forbidden flags, protected folders, the recipient rule for
reply drafts, the limits on sizes), so that code developed against it does not come to rely on
things that the real gateway refuses. Where the contract leaves a choice, the fake is strict.
"""

from __future__ import annotations

import argparse
import email
import email.header
import email.policy
import email.utils
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage, Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

VERSION = "0.1.0-fake"

ROLES = ("inbox", "drafts", "sent", "trash", "junk", "archive", "all", "flagged")
PROTECTED_ROLES = frozenset({"drafts", "sent"})
PROTECTED_NAMES = frozenset(
    {
        "drafts",
        "draft",
        "sent",
        "sent items",
        "sent mail",
        "sent messages",
        "outbox",
        "templates",
        "scheduled",
    }
)
SYSTEM_FLAGS_ALLOWED = frozenset({"\\Seen", "\\Flagged", "\\Answered"})
KEYWORD_RE = re.compile(r"[A-Za-z$][A-Za-z0-9$_.-]{0,63}\Z")
ACCOUNT_ID_RE = re.compile(r"[a-z0-9-]{1,32}\Z")

MAX_LIST_LIMIT = 2000
MAX_FETCH_UIDS = 100
MAX_MUTATION_UIDS = 500
MAX_SEARCH_STRING = 200
MAX_SEARCH_DEPTH = 5
MAX_SUBQUERIES = 20
MAX_SUBJECT = 500
MAX_BODY = 100_000
MAX_RECIPIENTS = 50
MAX_CHANGE_REQUEST_TEXT = 4000
MAX_PENDING_CHANGE_REQUESTS = 5
MAX_WAIT_SECONDS = 60
DELETION_RATE_LIMIT = 20
DELETION_RATE_WINDOW = timedelta(minutes=60)

FETCH_CONTENT_TYPE = "application/vnd.triage.messages"

_STATUS_BY_CODE = {
    "bad_request": 400,
    "forbidden": 403,
    "not_found": 404,
    "uid_validity_changed": 409,
    "unsupported": 501,
    "imap_error": 502,
    "unavailable": 503,
}

LlmHandler = Callable[[str, str, bytes], "FakeResponse"]


class ApiError(Exception):
    """An error response of the API."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = _STATUS_BY_CODE.get(code, 500)

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


@dataclass
class FakeResponse:
    status: int
    headers: dict[str, str]
    body: bytes | Iterator[bytes]

    @classmethod
    def json(cls, payload: object, status: int = 200) -> FakeResponse:
        data = json.dumps(payload).encode()
        return cls(status, {"content-type": "application/json"}, data)


@dataclass
class FakeMessage:
    uid: int
    raw: bytes
    flags: set[str]
    internal_date: datetime


@dataclass
class FakeFolder:
    name: str
    role: str | None = None
    delimiter: str | None = "/"
    selectable: bool = True
    protected: bool = False
    uid_validity: int = 1
    uid_next: int = 1
    messages: dict[int, FakeMessage] = field(default_factory=dict)


@dataclass
class FakeAccount:
    id: str
    address: str
    display_name: str = ""
    folders: dict[str, FakeFolder] = field(default_factory=dict)


@dataclass
class Fault:
    """Makes matching operations fail. ``None`` fields match anything."""

    code: str = "unavailable"
    message: str = "injected fault"
    op: str | None = None  # e.g. "messages/fetch", "folders/list"
    account: str | None = None
    folder: str | None = None
    remaining: int | None = None  # number of times to fire; None means always

    def matches(self, op: str, account: str | None, folder: str | None) -> bool:
        return (
            (self.op is None or self.op == op)
            and (self.account is None or self.account == account)
            and (self.folder is None or self.folder == folder)
        )


@dataclass
class _ChangeRequest:
    id: str
    kind: str
    text: str
    state: str = "pending"


class FakeGateway:
    """In-memory model of mailboxes plus the sandbox API on top of it.

    The methods without a leading underscore that are not part of the HTTP surface (``add_*``,
    ``delete_message`` and so on) are for tests: they change the "server" directly, as another
    mail client or the user would, without going through the API and without being logged.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.accounts: dict[str, FakeAccount] = {}
        self.log_entries: list[dict[str, Any]] = []
        self.change_requests: dict[str, _ChangeRequest] = {}
        self.coding_event_list: list[dict[str, Any]] = []
        self.event_list: list[dict[str, Any]] = []
        self.faults: list[Fault] = []
        self.llm_handler: LlmHandler | None = None
        # Fault injection for the message stream: cut the stream after this many messages
        # (no end frame), or end it with an error frame after this many messages.
        self.truncate_fetch_after: int | None = None
        self.fetch_error_after: tuple[int, str] | None = None
        self.requests: list[tuple[str, str]] = []  # (method, path) of every request handled
        self._deletions: dict[str, list[datetime]] = {}
        self._deletion_warned: dict[str, datetime] = {}
        self._next_uid_validity = 1000

    # -- setting up and changing the "server" directly -----------------------------------

    def add_account(
        self,
        account_id: str,
        address: str,
        display_name: str = "",
        *,
        default_folders: bool = True,
    ) -> FakeAccount:
        if not ACCOUNT_ID_RE.match(account_id):
            raise ValueError(f"invalid account id {account_id!r}")
        with self._lock:
            account = FakeAccount(account_id, address, display_name)
            self.accounts[account_id] = account
            if default_folders:
                for name, role in (
                    ("INBOX", "inbox"),
                    ("Drafts", "drafts"),
                    ("Sent", "sent"),
                    ("Archive", "archive"),
                    ("Junk", "junk"),
                    ("Trash", "trash"),
                ):
                    self.add_folder(account_id, name, role=role)
            self._emit_event({"kind": "accounts_changed"})
            return account

    def remove_account(self, account_id: str) -> None:
        with self._lock:
            del self.accounts[account_id]
            self._emit_event({"kind": "accounts_changed"})

    def add_folder(
        self,
        account_id: str,
        name: str,
        *,
        role: str | None = None,
        selectable: bool = True,
        protected: bool | None = None,
        delimiter: str | None = "/",
    ) -> FakeFolder:
        if role is not None and role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        with self._lock:
            if protected is None:
                protected = role in PROTECTED_ROLES or _leaf(name, delimiter) in PROTECTED_NAMES
            folder = FakeFolder(
                name=name,
                role=role,
                delimiter=delimiter,
                selectable=selectable,
                protected=protected,
                uid_validity=self._new_uid_validity(),
            )
            self.accounts[account_id].folders[name] = folder
            return folder

    def remove_folder(self, account_id: str, name: str) -> None:
        with self._lock:
            del self.accounts[account_id].folders[name]

    def folder(self, account_id: str, name: str) -> FakeFolder:
        return self.accounts[account_id].folders[name]

    def add_message(
        self,
        account_id: str,
        folder_name: str,
        raw: bytes,
        *,
        flags: Iterable[str] = (),
        internal_date: datetime | None = None,
    ) -> int:
        """Deliver a message into a folder. Returns its UID."""
        with self._lock:
            folder = self.accounts[account_id].folders[folder_name]
            uid = folder.uid_next
            folder.uid_next += 1
            when = internal_date or self._clock()
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            folder.messages[uid] = FakeMessage(uid, bytes(raw), set(flags), when)
            return uid

    def delete_message(self, account_id: str, folder_name: str, uid: int) -> None:
        """Expunge a message, as another mail client could."""
        with self._lock:
            del self.accounts[account_id].folders[folder_name].messages[uid]

    def set_flags_directly(
        self, account_id: str, folder_name: str, uid: int, flags: Iterable[str]
    ) -> None:
        with self._lock:
            self.accounts[account_id].folders[folder_name].messages[uid].flags = set(flags)

    def move_directly(self, account_id: str, folder_name: str, uid: int, destination: str) -> int:
        """Move a message as another mail client could. Returns the new UID."""
        with self._lock:
            source = self.accounts[account_id].folders[folder_name]
            message = source.messages.pop(uid)
            return self.add_message(
                account_id,
                destination,
                message.raw,
                flags=message.flags,
                internal_date=message.internal_date,
            )

    def reset_uid_validity(self, account_id: str, folder_name: str) -> None:
        """Simulate the folder being recreated: new ``uid_validity``, messages renumbered."""
        with self._lock:
            folder = self.accounts[account_id].folders[folder_name]
            old = [folder.messages[uid] for uid in sorted(folder.messages)]
            folder.uid_validity = self._new_uid_validity()
            folder.messages = {}
            folder.uid_next = 1
            for message in old:
                uid = folder.uid_next
                folder.uid_next += 1
                folder.messages[uid] = FakeMessage(
                    uid, message.raw, set(message.flags), message.internal_date
                )

    def decide_change_request(self, request_id: str, approve: bool) -> None:
        """Play the part of the user approving or rejecting a change request."""
        with self._lock:
            request = self.change_requests[request_id]
            request.state = "approved" if approve else "rejected"
            self._emit_event({"kind": "change_request", "id": request.id, "state": request.state})

    def push_coding_event(self, kind: str, text: str, request_id: str | None = None) -> None:
        """Play the part of the wrapper app relaying output of the coding agent."""
        with self._lock:
            self.coding_event_list.append(
                {
                    "seq": len(self.coding_event_list) + 1,
                    "time": _timestamp(self._clock()),
                    "request_id": request_id,
                    "kind": kind,
                    "text": text,
                }
            )
            self._changed.notify_all()

    def add_fault(self, fault: Fault) -> Fault:
        with self._lock:
            self.faults.append(fault)
            return fault

    def clear_faults(self) -> None:
        with self._lock:
            self.faults.clear()
            self.truncate_fetch_after = None
            self.fetch_error_after = None

    # -- ways of talking to the fake -----------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        """An ``httpx`` transport that serves requests from this fake, in-process."""

        def handler(request: httpx.Request) -> httpx.Response:
            target = request.url.raw_path.decode("ascii")
            response = self.handle(request.method, target, request.content)
            if isinstance(response.body, bytes):
                return httpx.Response(
                    response.status, headers=response.headers, content=response.body
                )
            return httpx.Response(
                response.status, headers=response.headers, stream=_IteratorStream(response.body)
            )

        return httpx.MockTransport(handler)

    def handle(self, method: str, target: str, body: bytes = b"") -> FakeResponse:
        """Handle one HTTP request. ``target`` is the path including any query string."""
        parts = urlsplit(target)
        path = parts.path
        query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        with self._lock:
            self.requests.append((method, path))
        try:
            return self._route(method.upper(), path, query, body)
        except ApiError as exc:
            return FakeResponse.json(exc.payload(), exc.status)

    # -- routing -------------------------------------------------------------------------

    def _route(self, method: str, path: str, query: dict[str, str], body: bytes) -> FakeResponse:
        if path.startswith("/v1/llm/") or path == "/v1/llm":
            return self._llm(method, path, body)

        get_routes: dict[str, Callable[[dict[str, str]], object]] = {
            "/v1/health": lambda _q: {"status": "ok", "version": VERSION},
            "/v1/accounts": self._accounts,
            "/v1/log": self._log,
            "/v1/coding/events": lambda q: self._poll(self.coding_event_list, q),
            "/v1/events": lambda q: self._poll(self.event_list, q),
        }
        post_routes: dict[str, Callable[[dict[str, Any]], object]] = {
            "/v1/folders/list": self._folders_list,
            "/v1/folders/create": self._folders_create,
            "/v1/folders/status": self._folders_status,
            "/v1/messages/list": self._messages_list,
            "/v1/messages/search": self._messages_search,
            "/v1/messages/flags": self._messages_flags,
            "/v1/messages/move": self._messages_move,
            "/v1/drafts": self._drafts,
        }

        if path in get_routes:
            _require_method(method, "GET")
            return FakeResponse.json(get_routes[path](query))
        if path == "/v1/messages/fetch":
            _require_method(method, "POST")
            return self._messages_fetch(_json_body(body))
        if path == "/v1/change-requests":
            _require_method(method, "POST")
            return FakeResponse.json(self._change_request_submit(_json_body(body)), 202)
        if path.startswith("/v1/change-requests/"):
            _require_method(method, "GET")
            return FakeResponse.json(self._change_request_get(path.rsplit("/", 1)[1]))
        if path in post_routes:
            _require_method(method, "POST")
            return FakeResponse.json(post_routes[path](_json_body(body)))
        raise ApiError("not_found", f"no such endpoint: {path}")

    # -- helpers -------------------------------------------------------------------------

    def _new_uid_validity(self) -> int:
        self._next_uid_validity += 1
        return self._next_uid_validity

    def _check_faults(self, op: str, account: str | None, folder: str | None) -> None:
        for fault in list(self.faults):
            if fault.matches(op, account, folder):
                if fault.remaining is not None:
                    fault.remaining -= 1
                    if fault.remaining <= 0:
                        self.faults.remove(fault)
                raise ApiError(fault.code, fault.message)

    def _account(self, request: Mapping[str, Any], op: str) -> FakeAccount:
        account_id = _field(request, "account", str)
        self._check_faults(op, account_id, None)
        account = self.accounts.get(account_id)
        if account is None:
            raise ApiError("not_found", f"unknown account {account_id!r}")
        return account

    def _folder(
        self, request: Mapping[str, Any], op: str, key: str = "folder"
    ) -> tuple[FakeAccount, FakeFolder]:
        account_id = _field(request, "account", str)
        name = _field(request, key, str)
        self._check_faults(op, account_id, name)
        account = self.accounts.get(account_id)
        if account is None:
            raise ApiError("not_found", f"unknown account {account_id!r}")
        folder = account.folders.get(name)
        if folder is None:
            raise ApiError("not_found", f"unknown folder {name!r}")
        if not folder.selectable:
            raise ApiError("bad_request", f"folder {name!r} cannot hold messages")
        return account, folder

    @staticmethod
    def _check_uid_validity(request: Mapping[str, Any], folder: FakeFolder) -> None:
        given = _field(request, "uid_validity", int)
        if given != folder.uid_validity:
            raise ApiError(
                "uid_validity_changed",
                f"uid_validity of {folder.name!r} is {folder.uid_validity}, not {given}",
            )

    def _append_log(self, entry: dict[str, Any]) -> None:
        entry = {"seq": len(self.log_entries) + 1, "time": _timestamp(self._clock()), **entry}
        self.log_entries.append(entry)

    def _emit_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.event_list.append(
                {"seq": len(self.event_list) + 1, "time": _timestamp(self._clock()), **event}
            )
            self._changed.notify_all()

    # -- accounts and folders ------------------------------------------------------------

    def _accounts(self, _query: dict[str, str]) -> object:
        with self._lock:
            self._check_faults("accounts", None, None)
            return {
                "accounts": [
                    {"id": a.id, "address": a.address, "display_name": a.display_name}
                    for a in self.accounts.values()
                ]
            }

    def _folders_list(self, request: dict[str, Any]) -> object:
        with self._lock:
            account = self._account(request, "folders/list")
            return {
                "folders": [
                    {
                        "name": f.name,
                        "delimiter": f.delimiter,
                        "role": f.role,
                        "selectable": f.selectable,
                        "protected": f.protected,
                    }
                    for f in account.folders.values()
                ]
            }

    def _folders_create(self, request: dict[str, Any]) -> object:
        with self._lock:
            account = self._account(request, "folders/create")
            name = _field(request, "name", str)
            if not name or len(name) > 200 or _has_control_chars(name):
                raise ApiError("bad_request", "invalid folder name")
            protected_names = PROTECTED_NAMES | {
                _leaf(f.name, f.delimiter) for f in account.folders.values() if f.protected
            }
            if any(part.strip().casefold() in protected_names for part in name.split("/")):
                raise ApiError("forbidden", "the name collides with a protected folder name")
            if name in account.folders or name.casefold() == "inbox":
                raise ApiError("imap_error", f"folder {name!r} already exists")
            self.add_folder(account.id, name, protected=False)
            self._append_log({"account": account.id, "op": "create_folder", "folder": name})
            return {}

    def _folders_status(self, request: dict[str, Any]) -> object:
        with self._lock:
            _account, folder = self._folder(request, "folders/status")
            return {
                "uid_validity": folder.uid_validity,
                "uid_next": folder.uid_next,
                "messages": len(folder.messages),
                "unseen": sum(1 for m in folder.messages.values() if "\\Seen" not in m.flags),
            }

    # -- reading messages ----------------------------------------------------------------

    def _messages_list(self, request: dict[str, Any]) -> object:
        with self._lock:
            _account, folder = self._folder(request, "messages/list")
            uid_min = _field(request, "uid_min", int, default=1)
            uid_max = _field(request, "uid_max", int, default=None, nullable=True)
            limit = _field(request, "limit", int, default=MAX_LIST_LIMIT)
            if uid_min < 1 or (uid_max is not None and uid_max < 1):
                raise ApiError("bad_request", "UIDs start at 1")
            if not 1 <= limit <= MAX_LIST_LIMIT:
                raise ApiError("bad_request", f"limit must be between 1 and {MAX_LIST_LIMIT}")
            uids = sorted(
                uid
                for uid in folder.messages
                if uid >= uid_min and (uid_max is None or uid <= uid_max)
            )
            page, rest = uids[:limit], uids[limit:]
            return {
                "uid_validity": folder.uid_validity,
                "messages": [_meta(folder.messages[uid]) for uid in page],
                "next_uid": rest[0] if rest else None,
            }

    def _messages_fetch(self, request: dict[str, Any]) -> FakeResponse:
        with self._lock:
            _account, folder = self._folder(request, "messages/fetch")
            uids = _uid_list(request, MAX_FETCH_UIDS)
            # Snapshot under the lock; the frames are produced lazily afterwards.
            found = [
                (_meta(folder.messages[uid]), folder.messages[uid].raw)
                for uid in dict.fromkeys(uids)
                if uid in folder.messages
            ]
            uid_validity = folder.uid_validity
            truncate_after = self.truncate_fetch_after
            error_after = self.fetch_error_after

        def frames() -> Iterator[bytes]:
            for index, (meta, raw) in enumerate(found):
                if truncate_after is not None and index >= truncate_after:
                    return
                if error_after is not None and index >= error_after[0]:
                    error = ApiError(error_after[1], "injected stream error")
                    yield json.dumps(error.payload()).encode() + b"\n"
                    return
                yield json.dumps(meta).encode() + b"\n"
                # Deliberately split the message into pieces, so that clients are exercised
                # on frames that straddle chunk boundaries.
                middle = len(raw) // 2
                yield raw[:middle]
                yield raw[middle:] + b"\n"
            if truncate_after is not None and len(found) >= truncate_after:
                return
            if error_after is not None and len(found) >= error_after[0]:
                error = ApiError(error_after[1], "injected stream error")
                yield json.dumps(error.payload()).encode() + b"\n"
                return
            yield json.dumps({"end": True, "uid_validity": uid_validity}).encode() + b"\n"

        return FakeResponse(200, {"content-type": FETCH_CONTENT_TYPE}, frames())

    def _messages_search(self, request: dict[str, Any]) -> object:
        with self._lock:
            _account, folder = self._folder(request, "messages/search")
            query = _field(request, "query", dict)
            _validate_query(query, 1)
            uids = sorted(uid for uid, m in folder.messages.items() if _matches(query, m))
            return {"uid_validity": folder.uid_validity, "uids": uids}

    # -- changing mailboxes --------------------------------------------------------------

    def _messages_flags(self, request: dict[str, Any]) -> object:
        with self._lock:
            account, folder = self._folder(request, "messages/flags")
            uids = _uid_list(request, MAX_MUTATION_UIDS)
            add = _flag_list(request, "add")
            remove = _flag_list(request, "remove")
            if not add and not remove:
                raise ApiError("bad_request", "no flags to add or remove")
            self._check_uid_validity(request, folder)
            for uid in uids:
                message = folder.messages.get(uid)
                if message is not None:
                    message.flags |= set(add)
                    message.flags -= set(remove)
            self._append_log(
                {
                    "account": account.id,
                    "op": "flags",
                    "folder": folder.name,
                    "uid_validity": folder.uid_validity,
                    "uids": uids,
                    "add": add,
                    "remove": remove,
                }
            )
            return {}

    def _messages_move(self, request: dict[str, Any]) -> object:
        with self._lock:
            account, folder = self._folder(request, "messages/move")
            uids = _uid_list(request, MAX_MUTATION_UIDS)
            destination_name = _field(request, "destination", str)
            destination = account.folders.get(destination_name)
            if destination is None:
                raise ApiError("not_found", f"unknown folder {destination_name!r}")
            if destination.protected:
                raise ApiError("forbidden", f"messages cannot be moved into {destination_name!r}")
            if not destination.selectable:
                raise ApiError("bad_request", f"folder {destination_name!r} cannot hold messages")
            if destination is folder:
                raise ApiError("bad_request", "source and destination are the same folder")
            self._check_uid_validity(request, folder)
            moved = []
            for uid in dict.fromkeys(uids):
                message = folder.messages.pop(uid, None)
                if message is None:
                    continue
                new_uid = destination.uid_next
                destination.uid_next += 1
                destination.messages[new_uid] = FakeMessage(
                    new_uid, message.raw, message.flags, message.internal_date
                )
                moved.append({"uid": uid, "new_uid": new_uid})
            self._append_log(
                {
                    "account": account.id,
                    "op": "move",
                    "folder": folder.name,
                    "uid_validity": folder.uid_validity,
                    "destination": destination.name,
                    "destination_uid_validity": destination.uid_validity,
                    "moved": moved,
                }
            )
            if destination.role in ("trash", "junk") and moved:
                self._count_deletions(account.id, len(moved))
            return {"destination_uid_validity": destination.uid_validity, "moved": moved}

    def _count_deletions(self, account_id: str, count: int) -> None:
        now = self._clock()
        recent = [t for t in self._deletions.get(account_id, []) if now - t < DELETION_RATE_WINDOW]
        recent.extend([now] * count)
        self._deletions[account_id] = recent
        last_warning = self._deletion_warned.get(account_id)
        recently_warned = last_warning is not None and now - last_warning < DELETION_RATE_WINDOW
        if len(recent) > DELETION_RATE_LIMIT and not recently_warned:
            self._deletion_warned[account_id] = now
            self._emit_event(
                {
                    "kind": "deletion_rate",
                    "account": account_id,
                    "count": len(recent),
                    "window_minutes": int(DELETION_RATE_WINDOW.total_seconds() // 60),
                }
            )

    def _drafts(self, request: dict[str, Any]) -> object:
        with self._lock:
            account = self._account(request, "drafts")
            to = _recipients(request, "to")
            cc = _recipients(request, "cc", required=False)
            subject = _field(request, "subject", str)
            body = _field(request, "body", str)
            if not to:
                raise ApiError("bad_request", "at least one recipient is needed in 'to'")
            if len(to) + len(cc) > MAX_RECIPIENTS:
                raise ApiError("bad_request", f"at most {MAX_RECIPIENTS} recipients")
            if len(subject) > MAX_SUBJECT or _has_control_chars(subject):
                raise ApiError("bad_request", "invalid subject")
            if len(body) > MAX_BODY:
                raise ApiError("bad_request", f"body is longer than {MAX_BODY} characters")
            unknown = set(request) - {"account", "to", "cc", "subject", "body", "in_reply_to"}
            if unknown:
                raise ApiError("bad_request", f"unknown fields: {', '.join(sorted(unknown))}")

            original: Message | None = None
            reference = request.get("in_reply_to")
            if reference is not None:
                if not isinstance(reference, dict):
                    raise ApiError("bad_request", "in_reply_to must be an object")
                _acct, source = self._folder(
                    {"account": account.id, "folder": _field(reference, "folder", str)}, "drafts"
                )
                self._check_uid_validity(reference, source)
                source_message = source.messages.get(_field(reference, "uid", int))
                if source_message is None:
                    raise ApiError("not_found", "the message being replied to does not exist")
                original = email.message_from_bytes(
                    source_message.raw, policy=email.policy.compat32
                )
                allowed = _participants(original)
                for recipient in (*to, *cc):
                    if recipient["address"].casefold() not in allowed:
                        raise ApiError(
                            "forbidden",
                            "recipients of a reply must be among the participants of the "
                            "message being replied to",
                        )

            drafts = next((f for f in account.folders.values() if f.role == "drafts"), None)
            if drafts is None:
                raise ApiError("unsupported", "the account has no drafts folder")

            message_id = f"<{uuid.uuid4().hex}@{account.address.rsplit('@', 1)[-1]}>"
            draft = EmailMessage()
            draft["From"] = email.utils.formataddr((account.display_name, account.address))
            draft["To"] = ", ".join(_format_recipient(r) for r in to)
            if cc:
                draft["Cc"] = ", ".join(_format_recipient(r) for r in cc)
            draft["Subject"] = subject
            draft["Date"] = email.utils.format_datetime(self._clock())
            draft["Message-ID"] = message_id
            if original is not None:
                original_id = _message_ids(str(original.get("Message-ID", "")))[:1]
                references = _message_ids(str(original.get("References", ""))) + original_id
                if original_id:
                    draft["In-Reply-To"] = original_id[0]
                if references:
                    draft["References"] = " ".join(references)
            draft.set_content(body)
            self.add_message(
                account.id,
                drafts.name,
                draft.as_bytes(policy=email.policy.SMTP),
                flags={"\\Draft", "\\Seen"},
            )
            entry: dict[str, Any] = {
                "account": account.id,
                "op": "draft",
                "folder": drafts.name,
                "message_id": message_id,
            }
            if reference is not None:
                entry["in_reply_to"] = {
                    "folder": reference["folder"],
                    "uid_validity": reference["uid_validity"],
                    "uid": reference["uid"],
                }
            self._append_log(entry)
            return {"folder": drafts.name, "message_id": message_id}

    def _log(self, query: dict[str, str]) -> object:
        with self._lock:
            after = _query_int(query, "after", 0)
            limit = _query_int(query, "limit", 500)
            if after < 0 or not 1 <= limit <= 1000:
                raise ApiError("bad_request", "after must be >= 0 and limit between 1 and 1000")
            entries = [e for e in self.log_entries if e["seq"] > after][:limit]
            return {"entries": entries, "last_seq": len(self.log_entries)}

    # -- change requests and events ------------------------------------------------------

    def _change_request_submit(self, request: dict[str, Any]) -> object:
        with self._lock:
            kind = _field(request, "kind", str)
            text = _field(request, "text", str)
            if kind not in ("feature", "bug", "reply"):
                raise ApiError("bad_request", f"unknown kind {kind!r}")
            if not text.strip() or len(text) > MAX_CHANGE_REQUEST_TEXT:
                raise ApiError(
                    "bad_request", f"text must have 1 to {MAX_CHANGE_REQUEST_TEXT} characters"
                )
            if _has_control_chars(text, allowed="\n\t"):
                raise ApiError("bad_request", "text contains control characters")
            pending = sum(1 for r in self.change_requests.values() if r.state == "pending")
            if pending >= MAX_PENDING_CHANGE_REQUESTS:
                raise ApiError("forbidden", "too many change requests are pending")
            request_id = f"cr-{uuid.uuid4().hex[:16]}"
            self.change_requests[request_id] = _ChangeRequest(request_id, kind, text)
            return {"id": request_id, "state": "pending"}

    def _change_request_get(self, request_id: str) -> object:
        with self._lock:
            request = self.change_requests.get(request_id)
            if request is None:
                raise ApiError("not_found", "unknown change request")
            return {"id": request.id, "kind": request.kind, "state": request.state}

    def _poll(self, events: list[dict[str, Any]], query: dict[str, str]) -> object:
        after = _query_int(query, "after", 0)
        wait = _query_float(query, "wait", 0.0)
        if after < 0 or not 0 <= wait <= MAX_WAIT_SECONDS:
            raise ApiError(
                "bad_request", f"after must be >= 0 and wait between 0 and {MAX_WAIT_SECONDS}"
            )
        deadline = time.monotonic() + wait
        with self._changed:
            while not any(e["seq"] > after for e in events):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            return {
                "events": [e for e in events if e["seq"] > after],
                "last_seq": len(events),
            }

    def _llm(self, method: str, path: str, body: bytes) -> FakeResponse:
        if method not in ("GET", "POST"):
            raise ApiError("bad_request", "only GET and POST are relayed to the LLM")
        handler = self.llm_handler
        if handler is None:
            raise ApiError("unavailable", "no local LLM is configured")
        return handler(method, path.removeprefix("/v1/llm"), body)


# --------------------------------------------------------------------------------------------
# Request validation


def _require_method(method: str, expected: str) -> None:
    if method != expected:
        raise ApiError("bad_request", f"this endpoint only accepts {expected}")


def _json_body(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise ApiError("bad_request", "the request body is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ApiError("bad_request", "the request body must be a JSON object")
    return parsed


_MISSING: Any = object()


def _field(
    request: Mapping[str, Any],
    name: str,
    kind: type,
    *,
    default: Any = _MISSING,
    nullable: bool = False,
) -> Any:
    value = request.get(name, _MISSING)
    if value is _MISSING or (value is None and default is not _MISSING):
        if default is _MISSING:
            raise ApiError("bad_request", f"missing field {name!r}")
        return default
    if value is None and nullable:
        return None
    if isinstance(value, bool) and kind is not bool:
        raise ApiError("bad_request", f"field {name!r} has the wrong type")
    if not isinstance(value, kind):
        raise ApiError("bad_request", f"field {name!r} has the wrong type")
    return value


def _query_int(query: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(query[name]) if name in query else default
    except ValueError as exc:
        raise ApiError("bad_request", f"query parameter {name!r} is not an integer") from exc


def _query_float(query: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(query[name]) if name in query else default
    except ValueError as exc:
        raise ApiError("bad_request", f"query parameter {name!r} is not a number") from exc


def _uid_list(request: Mapping[str, Any], maximum: int) -> list[int]:
    uids: list[int] = _field(request, "uids", list)
    if not 1 <= len(uids) <= maximum:
        raise ApiError("bad_request", f"between 1 and {maximum} UIDs are needed")
    for uid in uids:
        if isinstance(uid, bool) or not isinstance(uid, int) or not 1 <= uid < 2**32:
            raise ApiError("bad_request", "UIDs must be positive integers")
    return uids


def _flag_list(request: Mapping[str, Any], name: str) -> list[str]:
    flags: list[str] = _field(request, name, list, default=[])
    for flag in flags:
        if not isinstance(flag, str):
            raise ApiError("bad_request", "flags must be strings")
        if flag.startswith("\\"):
            if flag not in SYSTEM_FLAGS_ALLOWED:
                raise ApiError("forbidden", f"the flag {flag} cannot be changed")
        elif not KEYWORD_RE.match(flag):
            raise ApiError("bad_request", f"invalid keyword {flag!r}")
    return flags


_ADDRESS_RE = re.compile(r"[^\s@<>(),;:\\\"\[\]]+@[^\s@<>(),;:\\\"\[\]]+\Z")


def _recipients(
    request: Mapping[str, Any], name: str, required: bool = True
) -> list[dict[str, str]]:
    raw = _field(request, name, list) if required else _field(request, name, list, default=[])
    recipients = []
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError("bad_request", "recipients must be objects")
        address = _field(item, "address", str)
        display = _field(item, "name", str, default="", nullable=True) or ""
        if not _ADDRESS_RE.match(address) or len(address) > 254 or not address.isascii():
            raise ApiError("bad_request", f"invalid address {address!r}")
        if len(display) > 200 or _has_control_chars(display):
            raise ApiError("bad_request", "invalid recipient name")
        recipients.append({"address": address, "name": display})
    return recipients


def _format_recipient(recipient: Mapping[str, str]) -> str:
    return email.utils.formataddr((recipient["name"], recipient["address"]))


def _has_control_chars(text: str, allowed: str = "") -> bool:
    return any((ord(c) < 32 or ord(c) == 127) and c not in allowed for c in text)


def _leaf(name: str, delimiter: str | None) -> str:
    leaf = name.rsplit(delimiter, 1)[-1] if delimiter else name
    return leaf.strip().casefold()


def _timestamp(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


def _meta(message: FakeMessage) -> dict[str, Any]:
    return {
        "uid": message.uid,
        "flags": sorted(message.flags),
        "size": len(message.raw),
        "internal_date": _timestamp(message.internal_date),
    }


_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


def _message_ids(value: str) -> list[str]:
    return _MESSAGE_ID_RE.findall(value)


def _participants(message: Message) -> set[str]:
    values: list[str] = []
    for header in ("From", "Sender", "Reply-To", "To", "Cc"):
        values.extend(str(v) for v in message.get_all(header, []))
    return {address.casefold() for _name, address in email.utils.getaddresses(values) if address}


# --------------------------------------------------------------------------------------------
# Search


_STRING_KEYS = ("from", "to", "cc", "subject", "body", "text", "keyword")
_DATE_KEYS = ("since", "before", "sent_since", "sent_before")
_BOOL_KEYS = ("seen", "flagged", "answered", "draft")
_INT_KEYS = ("larger", "smaller")


def _check_search_string(value: object) -> str:
    if not isinstance(value, str):
        raise ApiError("bad_request", "search strings must be strings")
    if len(value) > MAX_SEARCH_STRING:
        raise ApiError("bad_request", "search string is too long")
    if not value.isascii():
        raise ApiError("unsupported", "search strings must be ASCII for now")
    if not all(32 <= ord(c) < 127 for c in value):
        raise ApiError("bad_request", "search strings must be printable")
    return value


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise ApiError("bad_request", "dates must be strings")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError("bad_request", f"malformed date {value!r}") from exc


def _validate_query(query: object, depth: int) -> None:
    if depth > MAX_SEARCH_DEPTH:
        raise ApiError("bad_request", "the query is nested too deeply")
    if not isinstance(query, dict) or len(query) != 1:
        raise ApiError("bad_request", "a query is an object with exactly one key")
    ((key, value),) = query.items()
    if key == "all":
        if value is not True:
            raise ApiError("bad_request", "'all' must be true")
    elif key in ("and", "or"):
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_SUBQUERIES:
            raise ApiError("bad_request", f"{key!r} needs 1 to {MAX_SUBQUERIES} subqueries")
        for sub in value:
            _validate_query(sub, depth + 1)
    elif key == "not":
        _validate_query(value, depth + 1)
    elif key in _DATE_KEYS:
        _parse_date(value)
    elif key in _STRING_KEYS:
        _check_search_string(value)
        if key == "keyword" and not KEYWORD_RE.match(value):
            raise ApiError("bad_request", f"invalid keyword {value!r}")
    elif key == "header":
        if not isinstance(value, list) or len(value) != 2:
            raise ApiError("bad_request", "'header' needs a name and a value")
        name = _check_search_string(value[0])
        _check_search_string(value[1])
        if not re.fullmatch(r"[!-9;-~]+", name):
            raise ApiError("bad_request", "invalid header name")
    elif key in _BOOL_KEYS:
        if not isinstance(value, bool):
            raise ApiError("bad_request", f"{key!r} must be true or false")
    elif key in _INT_KEYS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ApiError("bad_request", f"{key!r} must be a non-negative integer")
    elif key == "uid":
        if (
            not isinstance(value, list)
            or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in value)
        ):
            raise ApiError("bad_request", "'uid' needs a pair of positive integers")
    else:
        raise ApiError("bad_request", f"unknown query key {key!r}")


def _header_text(message: Message, name: str) -> str:
    parts = []
    for value in message.get_all(name, []):
        try:
            parts.append(str(email.header.make_header(email.header.decode_header(str(value)))))
        except Exception:
            parts.append(str(value))
    return " ".join(parts).casefold()


def _body_text(message: Message) -> str:
    texts = []
    for part in message.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            texts.append(payload.decode("utf-8", "replace"))
    return "\n".join(texts).casefold()


def _sent_date(message: Message) -> date | None:
    try:
        return email.utils.parsedate_to_datetime(str(message.get("Date"))).date()
    except (TypeError, ValueError):
        return None


def _matches(query: Mapping[str, Any], message: FakeMessage) -> bool:
    parsed: Message | None = None

    def mime() -> Message:
        nonlocal parsed
        if parsed is None:
            parsed = email.message_from_bytes(message.raw, policy=email.policy.compat32)
        return parsed

    def evaluate(q: Mapping[str, Any]) -> bool:
        ((key, value),) = q.items()
        if key == "all":
            return True
        if key == "and":
            return all(evaluate(sub) for sub in value)
        if key == "or":
            return any(evaluate(sub) for sub in value)
        if key == "not":
            return not evaluate(value)
        if key == "since":
            return message.internal_date.astimezone(UTC).date() >= _parse_date(value)
        if key == "before":
            return message.internal_date.astimezone(UTC).date() < _parse_date(value)
        if key in ("sent_since", "sent_before"):
            sent = _sent_date(mime())
            if sent is None:
                return False
            return sent >= _parse_date(value) if key == "sent_since" else sent < _parse_date(value)
        if key in ("from", "to", "cc", "subject"):
            return bool(value.casefold() in _header_text(mime(), key))
        if key == "header":
            return bool(value[1].casefold() in _header_text(mime(), value[0]))
        if key == "body":
            return bool(value.casefold() in _body_text(mime()))
        if key == "text":
            needle = value.casefold()
            return needle in message.raw.decode("utf-8", "replace").casefold() or needle in (
                _body_text(mime())
            )
        if key == "keyword":
            return value in message.flags
        if key in _BOOL_KEYS:
            flag = "\\" + key.capitalize()
            return bool((flag in message.flags) == value)
        if key == "larger":
            return bool(len(message.raw) > value)
        if key == "smaller":
            return bool(len(message.raw) < value)
        if key == "uid":
            return bool(value[0] <= message.uid <= value[1])
        raise ApiError("bad_request", f"unknown query key {key!r}")

    return evaluate(query)


# --------------------------------------------------------------------------------------------
# Transports


class _IteratorStream(httpx.SyncByteStream):
    def __init__(self, chunks: Iterator[bytes]):
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks


def serve(
    fake: FakeGateway, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:  # pragma: no cover - exercised through test_fake_gateway_server
    """Create an HTTP server for the fake. Call ``serve_forever()`` on the result.

    The port actually bound is ``server.server_address[1]``.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            response = fake.handle(self.command, self.path, body)
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            if isinstance(response.body, bytes):
                self.send_header("content-length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)
            else:
                # A stream of unknown length is delimited by closing the connection. The
                # message stream has its own end frame, so a client can tell if it was cut.
                self.send_header("connection", "close")
                self.end_headers()
                for chunk in response.body:
                    self.wfile.write(chunk)
                self.close_connection = True

        do_GET = _respond
        do_POST = _respond
        do_PUT = _respond
        do_DELETE = _respond

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> None:  # pragma: no cover
    """Run a fake gateway filled with synthetic demo mail."""
    from triage.testing.samples import demo_gateway

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)
    server = serve(demo_gateway(), args.host, args.port)
    print(f"fake gateway listening on http://{args.host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()

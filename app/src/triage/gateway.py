"""Client for the gateway's sandbox API.

The gateway is the email sandbox's only network peer. The contract is described in
``docs/gateway-api.md``; this module is a thin, typed wrapper around it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import TracebackType
from typing import Any, Literal, Self

import httpx

DEFAULT_GATEWAY_URL = "http://gateway:8100"

MAX_LIST_LIMIT = 2000
MAX_FETCH_UIDS = 100
MAX_MUTATION_UIDS = 500

# A frame header is one short line of JSON. Anything much longer means that the stream is not
# what we think it is, and we must not buffer it without bound.
_MAX_FRAME_HEADER = 64 * 1024

FETCH_CONTENT_TYPE = "application/vnd.triage.messages"

Query = Mapping[str, Any]
ChangeRequestKind = Literal["feature", "bug", "reply"]


# --------------------------------------------------------------------------------------------
# Errors


class GatewayError(Exception):
    """An error reported by the gateway, or a failure to talk to it."""

    code: str = "error"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.status = status

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class BadRequestError(GatewayError):
    code = "bad_request"


class ForbiddenError(GatewayError):
    """The operation is not permitted by the gateway's rules."""

    code = "forbidden"


class NotFoundError(GatewayError):
    code = "not_found"


class UidValidityChangedError(GatewayError):
    """The folder was recreated on the server; cached UIDs are meaningless."""

    code = "uid_validity_changed"


class UnsupportedError(GatewayError):
    code = "unsupported"


class ImapError(GatewayError):
    """The IMAP server rejected the command."""

    code = "imap_error"


class UnavailableError(GatewayError):
    """The IMAP server or the local LLM cannot be reached, or login failed."""

    code = "unavailable"


class GatewayConnectionError(GatewayError):
    """The gateway itself could not be reached."""

    code = "gateway_unreachable"


class GatewayProtocolError(GatewayError):
    """The gateway sent something that does not conform to the contract."""

    code = "protocol_error"


class TruncatedStreamError(GatewayProtocolError):
    """A message stream ended without its ``end`` frame. The request can be retried."""

    code = "truncated_stream"


_ERRORS_BY_CODE: dict[str, type[GatewayError]] = {
    cls.code: cls
    for cls in (
        BadRequestError,
        ForbiddenError,
        NotFoundError,
        UidValidityChangedError,
        UnsupportedError,
        ImapError,
        UnavailableError,
    )
}


def error_from_payload(payload: object, status: int | None = None) -> GatewayError:
    """Build the exception for an ``{"error": {...}}`` body (or its inner object)."""
    if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping):
        payload = payload["error"]
    if not isinstance(payload, Mapping):
        return GatewayProtocolError(f"malformed error response (HTTP {status})", status=status)
    code = payload.get("code")
    message = payload.get("message")
    if not isinstance(code, str):
        return GatewayProtocolError(f"malformed error response (HTTP {status})", status=status)
    if not isinstance(message, str):
        message = ""
    cls = _ERRORS_BY_CODE.get(code, GatewayError)
    return cls(message, code=code, status=status)


# --------------------------------------------------------------------------------------------
# Data types


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    address: str
    display_name: str


@dataclass(frozen=True, slots=True)
class Folder:
    name: str
    delimiter: str | None
    role: str | None
    selectable: bool
    protected: bool


@dataclass(frozen=True, slots=True)
class FolderStatus:
    uid_validity: int
    uid_next: int
    messages: int
    unseen: int


@dataclass(frozen=True, slots=True)
class MessageMeta:
    uid: int
    flags: tuple[str, ...]
    size: int
    internal_date: datetime


@dataclass(frozen=True, slots=True)
class MessagePage:
    uid_validity: int
    messages: tuple[MessageMeta, ...]
    next_uid: int | None


@dataclass(frozen=True, slots=True)
class FetchedMessage:
    uid: int
    flags: tuple[str, ...]
    size: int
    internal_date: datetime
    raw: bytes

    @property
    def meta(self) -> MessageMeta:
        return MessageMeta(self.uid, self.flags, self.size, self.internal_date)


@dataclass(frozen=True, slots=True)
class SearchResult:
    uid_validity: int
    uids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MovedMessage:
    uid: int
    new_uid: int | None


@dataclass(frozen=True, slots=True)
class MoveResult:
    destination_uid_validity: int | None
    moved: tuple[MovedMessage, ...]


@dataclass(frozen=True, slots=True)
class Recipient:
    address: str
    name: str | None = None

    def to_json(self) -> dict[str, str]:
        out = {"address": self.address}
        if self.name:
            out["name"] = self.name
        return out


@dataclass(frozen=True, slots=True)
class MessageRef:
    """Identifies a message on the server."""

    folder: str
    uid_validity: int
    uid: int

    def to_json(self) -> dict[str, Any]:
        return {"folder": self.folder, "uid_validity": self.uid_validity, "uid": self.uid}


@dataclass(frozen=True, slots=True)
class DraftResult:
    folder: str
    message_id: str


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One entry of the operation log. ``data`` holds the complete entry as sent."""

    seq: int
    time: datetime
    account: str
    op: str
    data: Mapping[str, Any] = field(compare=False)


@dataclass(frozen=True, slots=True)
class LogPage:
    entries: tuple[LogEntry, ...]
    last_seq: int


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    id: str
    state: str
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class Event:
    """A notification or a piece of coding agent output. ``data`` is the complete event."""

    seq: int
    time: datetime
    kind: str
    data: Mapping[str, Any] = field(compare=False)

    @property
    def text(self) -> str:
        value = self.data.get("text", "")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class EventPage:
    events: tuple[Event, ...]
    last_seq: int


# --------------------------------------------------------------------------------------------
# Decoding helpers


def parse_timestamp(value: object) -> datetime:
    """Parse an RFC 3339 timestamp into an aware datetime in UTC."""
    if not isinstance(value, str):
        raise GatewayProtocolError(f"expected a timestamp, got {type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GatewayProtocolError(f"malformed timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _obj(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GatewayProtocolError(f"expected an object for {what}")
    return value


def _list(value: object, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise GatewayProtocolError(f"expected a list for {what}")
    return value


def _int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayProtocolError(f"expected an integer for {what}")
    return value


def _opt_int(value: object, what: str) -> int | None:
    return None if value is None else _int(value, what)


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise GatewayProtocolError(f"expected a string for {what}")
    return value


def _opt_str(value: object, what: str) -> str | None:
    return None if value is None else _str(value, what)


def _flags(value: object) -> tuple[str, ...]:
    return tuple(_str(f, "flag") for f in _list(value, "flags"))


def _meta(value: object) -> MessageMeta:
    obj = _obj(value, "message")
    return MessageMeta(
        uid=_int(obj.get("uid"), "uid"),
        flags=_flags(obj.get("flags")),
        size=_int(obj.get("size"), "size"),
        internal_date=parse_timestamp(obj.get("internal_date")),
    )


def _events(payload: Mapping[str, Any]) -> EventPage:
    events = []
    for raw in _list(payload.get("events"), "events"):
        obj = _obj(raw, "event")
        events.append(
            Event(
                seq=_int(obj.get("seq"), "seq"),
                time=parse_timestamp(obj.get("time")),
                kind=_str(obj.get("kind"), "kind"),
                data=dict(obj),
            )
        )
    return EventPage(tuple(events), _int(payload.get("last_seq"), "last_seq"))


def _date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else value


# --------------------------------------------------------------------------------------------
# Message stream


class MessageStream:
    """Iterates over the frames of a ``/v1/messages/fetch`` response.

    Messages are decoded one at a time as they arrive; the batch is never held in memory as a
    whole. After the iterator is exhausted, :attr:`uid_validity` holds the value from the
    ``end`` frame. If the stream ends without an ``end`` frame, :class:`TruncatedStreamError`
    is raised, and an ``error`` frame is raised as the corresponding :class:`GatewayError`.

    The stream holds an HTTP connection: use it as a context manager, or exhaust it.
    """

    def __init__(self, response: httpx.Response | None):
        self._response = response
        self._chunks: Iterator[bytes] | None = (
            response.iter_bytes() if response is not None else None
        )
        self._buffer = bytearray()
        self._finished = response is None
        self.uid_validity: int | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __iter__(self) -> Self:
        return self

    def close(self) -> None:
        self._finished = True
        self._chunks = None
        if self._response is not None:
            self._response.close()
            self._response = None

    def _fill(self) -> bool:
        """Read one more chunk into the buffer. Returns False at the end of the response."""
        if self._chunks is None:
            return False
        try:
            chunk = next(self._chunks)
        except StopIteration:
            return False
        except httpx.TransportError as exc:
            self.close()
            raise TruncatedStreamError(f"connection lost while reading messages: {exc}") from exc
        self._buffer += chunk
        return True

    def _read_line(self) -> bytes | None:
        """Read up to and including the next newline. None means a clean end of the data."""
        searched = 0
        while True:
            index = self._buffer.find(b"\n", searched)
            if index >= 0:
                line = bytes(self._buffer[:index])
                del self._buffer[: index + 1]
                return line
            searched = len(self._buffer)
            if searched > _MAX_FRAME_HEADER:
                raise GatewayProtocolError("frame header is too long")
            if not self._fill():
                if self._buffer:
                    raise TruncatedStreamError("message stream ended in the middle of a frame")
                return None

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            if not self._fill():
                raise TruncatedStreamError("message stream ended in the middle of a message")
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def __next__(self) -> FetchedMessage:
        if self._finished:
            raise StopIteration
        try:
            return self._next_frame()
        except StopIteration:
            self.close()
            raise
        except BaseException:
            self.close()
            raise

    def _next_frame(self) -> FetchedMessage:
        line = self._read_line()
        if line is None:
            raise TruncatedStreamError("message stream ended without an end frame")
        try:
            header = json.loads(line)
        except ValueError as exc:
            raise GatewayProtocolError("frame header is not valid JSON") from exc
        frame = _obj(header, "frame")
        if "error" in frame:
            raise error_from_payload(frame)
        if frame.get("end") is True:
            self.uid_validity = _opt_int(frame.get("uid_validity"), "uid_validity")
            raise StopIteration
        meta = _meta(frame)
        if meta.size < 0:
            raise GatewayProtocolError("negative message size")
        raw = self._read_exact(meta.size)
        if self._read_exact(1) != b"\n":
            raise GatewayProtocolError("message is not followed by a newline")
        return FetchedMessage(meta.uid, meta.flags, meta.size, meta.internal_date, raw)


# --------------------------------------------------------------------------------------------
# Client


class GatewayClient:
    """Synchronous client for the gateway's sandbox API.

    The client is safe to share between threads (``httpx.Client`` is).
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 120.0,
    ):
        if base_url is None:
            base_url = os.environ.get("TRIAGE_GATEWAY_URL", DEFAULT_GATEWAY_URL)
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        # trust_env=False: never pick up proxy settings from the environment.
        self._http = httpx.Client(
            base_url=self.base_url,
            transport=transport,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- plumbing ------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        try:
            response = self._http.request(
                method,
                path,
                json=body,
                params=params,
                timeout=self._timeout if timeout is None else timeout,
            )
        except httpx.TransportError as exc:
            raise GatewayConnectionError(f"cannot reach the gateway: {exc}") from exc
        return self._decode(response)

    @staticmethod
    def _decode(response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            if response.is_success:
                raise GatewayProtocolError("response is not valid JSON") from exc
            raise GatewayProtocolError(
                f"HTTP {response.status_code} without an error body", status=response.status_code
            ) from exc
        if not response.is_success:
            raise error_from_payload(payload, response.status_code)
        return _obj(payload, "response")

    def _get(
        self, path: str, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> Mapping[str, Any]:
        return self._request("GET", path, params=params, timeout=timeout)

    def _post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("POST", path, body=body)

    # -- accounts and folders ------------------------------------------------------------

    def health(self) -> Mapping[str, Any]:
        return self._get("/v1/health")

    def accounts(self) -> list[Account]:
        payload = self._get("/v1/accounts")
        accounts = []
        for raw in _list(payload.get("accounts"), "accounts"):
            obj = _obj(raw, "account")
            accounts.append(
                Account(
                    id=_str(obj.get("id"), "id"),
                    address=_str(obj.get("address"), "address"),
                    display_name=_opt_str(obj.get("display_name"), "display_name") or "",
                )
            )
        return accounts

    def list_folders(self, account: str) -> list[Folder]:
        payload = self._post("/v1/folders/list", {"account": account})
        folders = []
        for raw in _list(payload.get("folders"), "folders"):
            obj = _obj(raw, "folder")
            folders.append(
                Folder(
                    name=_str(obj.get("name"), "name"),
                    delimiter=_opt_str(obj.get("delimiter"), "delimiter"),
                    role=_opt_str(obj.get("role"), "role"),
                    selectable=bool(obj.get("selectable", True)),
                    protected=bool(obj.get("protected", False)),
                )
            )
        return folders

    def create_folder(self, account: str, name: str) -> None:
        self._post("/v1/folders/create", {"account": account, "name": name})

    def folder_status(self, account: str, folder: str) -> FolderStatus:
        payload = self._post("/v1/folders/status", {"account": account, "folder": folder})
        return FolderStatus(
            uid_validity=_int(payload.get("uid_validity"), "uid_validity"),
            uid_next=_int(payload.get("uid_next"), "uid_next"),
            messages=_int(payload.get("messages"), "messages"),
            unseen=_int(payload.get("unseen"), "unseen"),
        )

    # -- reading messages ----------------------------------------------------------------

    def list_messages(
        self,
        account: str,
        folder: str,
        *,
        uid_min: int = 1,
        uid_max: int | None = None,
        limit: int = MAX_LIST_LIMIT,
    ) -> MessagePage:
        """One page of message metadata in ascending UID order."""
        payload = self._post(
            "/v1/messages/list",
            {
                "account": account,
                "folder": folder,
                "uid_min": uid_min,
                "uid_max": uid_max,
                "limit": limit,
            },
        )
        return MessagePage(
            uid_validity=_int(payload.get("uid_validity"), "uid_validity"),
            messages=tuple(_meta(m) for m in _list(payload.get("messages"), "messages")),
            next_uid=_opt_int(payload.get("next_uid"), "next_uid"),
        )

    def iter_message_pages(
        self,
        account: str,
        folder: str,
        *,
        uid_min: int = 1,
        uid_max: int | None = None,
        page_size: int = MAX_LIST_LIMIT,
    ) -> Iterator[MessagePage]:
        """All pages of message metadata in the UID range, following ``next_uid``."""
        next_uid: int | None = uid_min
        while next_uid is not None:
            page = self.list_messages(
                account, folder, uid_min=next_uid, uid_max=uid_max, limit=page_size
            )
            if page.next_uid is not None and page.next_uid <= next_uid:
                raise GatewayProtocolError("next_uid does not advance")
            yield page
            next_uid = page.next_uid

    def fetch_messages(self, account: str, folder: str, uids: Sequence[int]) -> MessageStream:
        """Fetch the raw content of up to 100 messages as a stream.

        UIDs that do not exist are omitted, and messages may arrive in any order.
        """
        if len(uids) > MAX_FETCH_UIDS:
            raise BadRequestError(f"at most {MAX_FETCH_UIDS} messages can be fetched at once")
        if not uids:
            return MessageStream(None)
        request = self._http.build_request(
            "POST",
            "/v1/messages/fetch",
            json={"account": account, "folder": folder, "uids": list(uids)},
        )
        try:
            response = self._http.send(request, stream=True)
        except httpx.TransportError as exc:
            raise GatewayConnectionError(f"cannot reach the gateway: {exc}") from exc
        if not response.is_success:
            try:
                response.read()
            except httpx.TransportError as exc:
                raise GatewayConnectionError(f"cannot reach the gateway: {exc}") from exc
            finally:
                response.close()
            self._decode(response)
            raise GatewayProtocolError("unreachable")  # pragma: no cover
        return MessageStream(response)

    def search(self, account: str, folder: str, query: Query) -> SearchResult:
        """Server-side search. See :mod:`triage.gateway` ``q_*`` helpers for building queries."""
        payload = self._post(
            "/v1/messages/search", {"account": account, "folder": folder, "query": query}
        )
        return SearchResult(
            uid_validity=_int(payload.get("uid_validity"), "uid_validity"),
            uids=tuple(_int(u, "uid") for u in _list(payload.get("uids"), "uids")),
        )

    # -- changing mailboxes --------------------------------------------------------------

    def set_flags(
        self,
        account: str,
        folder: str,
        uid_validity: int,
        uids: Sequence[int],
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
    ) -> None:
        self._post(
            "/v1/messages/flags",
            {
                "account": account,
                "folder": folder,
                "uid_validity": uid_validity,
                "uids": list(uids),
                "add": list(add),
                "remove": list(remove),
            },
        )

    def move_messages(
        self, account: str, folder: str, uid_validity: int, uids: Sequence[int], destination: str
    ) -> MoveResult:
        payload = self._post(
            "/v1/messages/move",
            {
                "account": account,
                "folder": folder,
                "uid_validity": uid_validity,
                "uids": list(uids),
                "destination": destination,
            },
        )
        moved = []
        for raw in _list(payload.get("moved"), "moved"):
            obj = _obj(raw, "moved message")
            moved.append(
                MovedMessage(_int(obj.get("uid"), "uid"), _opt_int(obj.get("new_uid"), "new_uid"))
            )
        return MoveResult(
            destination_uid_validity=_opt_int(
                payload.get("destination_uid_validity"), "destination_uid_validity"
            ),
            moved=tuple(moved),
        )

    def create_draft(
        self,
        account: str,
        *,
        to: Sequence[Recipient],
        subject: str,
        body: str,
        cc: Sequence[Recipient] = (),
        in_reply_to: MessageRef | None = None,
    ) -> DraftResult:
        request: dict[str, Any] = {
            "account": account,
            "to": [r.to_json() for r in to],
            "cc": [r.to_json() for r in cc],
            "subject": subject,
            "body": body,
        }
        if in_reply_to is not None:
            request["in_reply_to"] = in_reply_to.to_json()
        payload = self._post("/v1/drafts", request)
        return DraftResult(
            folder=_str(payload.get("folder"), "folder"),
            message_id=_str(payload.get("message_id"), "message_id"),
        )

    def log(self, *, after: int = 0, limit: int = 500) -> LogPage:
        """A page of the operation log, oldest first."""
        payload = self._get("/v1/log", {"after": after, "limit": limit})
        entries = []
        for raw in _list(payload.get("entries"), "entries"):
            obj = _obj(raw, "log entry")
            entries.append(
                LogEntry(
                    seq=_int(obj.get("seq"), "seq"),
                    time=parse_timestamp(obj.get("time")),
                    account=_str(obj.get("account"), "account"),
                    op=_str(obj.get("op"), "op"),
                    data=dict(obj),
                )
            )
        return LogPage(tuple(entries), _int(payload.get("last_seq"), "last_seq"))

    # -- change requests and events ------------------------------------------------------

    def submit_change_request(self, kind: ChangeRequestKind, text: str) -> ChangeRequest:
        """Submit a message for the coding agent. Nothing is sent until the user approves it."""
        payload = self._post("/v1/change-requests", {"kind": kind, "text": text})
        return ChangeRequest(
            id=_str(payload.get("id"), "id"),
            state=_str(payload.get("state"), "state"),
            kind=_opt_str(payload.get("kind"), "kind") or kind,
        )

    def change_request(self, request_id: str) -> ChangeRequest:
        payload = self._get(f"/v1/change-requests/{request_id}")
        return ChangeRequest(
            id=_str(payload.get("id"), "id"),
            state=_str(payload.get("state"), "state"),
            kind=_opt_str(payload.get("kind"), "kind"),
        )

    def coding_events(self, *, after: int = 0, wait: float = 0) -> EventPage:
        """Output of the coding agent. Untrusted text: display it, never act on it."""
        return self._poll("/v1/coding/events", after, wait)

    def events(self, *, after: int = 0, wait: float = 0) -> EventPage:
        """Notifications from the trusted base."""
        return self._poll("/v1/events", after, wait)

    def _poll(self, path: str, after: int, wait: float) -> EventPage:
        wait = max(0.0, min(float(wait), 60.0))
        params: dict[str, Any] = {"after": after}
        if wait:
            params["wait"] = int(wait) if wait == int(wait) else wait
        return _events(self._get(path, params, timeout=self._timeout + wait))


# --------------------------------------------------------------------------------------------
# Query builders for the search operation


def q_all() -> Query:
    return {"all": True}


def q_and(*queries: Query) -> Query:
    return {"and": list(queries)}


def q_or(*queries: Query) -> Query:
    return {"or": list(queries)}


def q_not(query: Query) -> Query:
    return {"not": query}


def q_since(day: date | str) -> Query:
    """Messages that arrived on or after ``day``."""
    return {"since": _date(day)}


def q_before(day: date | str) -> Query:
    """Messages that arrived before ``day``."""
    return {"before": _date(day)}

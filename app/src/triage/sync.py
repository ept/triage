"""Keeps the local cache in step with the mailboxes on the server.

For every account, the whole inbox is cached, and of every other folder the messages that
arrived in the last few months. A sync run compares what the server has with what the cache has
and transfers the difference, so it is incremental, and a run that is interrupted loses nothing:
the next one picks up where it stopped.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Literal

from triage.gateway import (
    MAX_FETCH_UIDS,
    Account,
    FetchedMessage,
    Folder,
    GatewayClient,
    GatewayError,
    GatewayProtocolError,
    MessageMeta,
    UidValidityChangedError,
    q_since,
)
from triage.mime import ParsedMessage, parse_message
from triage.store import NewMessage, StaleFolderError, Store, StoreError

logger = logging.getLogger(__name__)

# Folders that only show messages which also live elsewhere (as on Gmail).
SKIPPED_ROLES = frozenset({"all", "flagged"})
# Roles in the order in which they are synced: what the user is most likely to ask about first.
_ROLE_ORDER = {"inbox": 0, "sent": 1, "drafts": 2, None: 3, "archive": 4, "junk": 5, "trash": 6}

FolderSyncState = Literal["pending", "listing", "fetching", "done", "error", "skipped"]
RunState = Literal["idle", "running"]


@dataclass(frozen=True, slots=True)
class SyncConfig:
    months: int = 3
    """How far back folders other than the inbox are cached."""
    batch_messages: int = MAX_FETCH_UIDS
    """The most messages requested from the gateway at once."""
    batch_bytes: int = 20 * 1024 * 1024
    """The most bytes requested at once; a single larger message is requested alone."""
    page_size: int = 2000


@dataclass(frozen=True, slots=True)
class FolderProgress:
    account: str
    folder: str
    state: FolderSyncState = "pending"
    messages_total: int = 0
    """Messages that this run has to download."""
    messages_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AccountProgress:
    account: str
    error: str | None = None
    folders: tuple[FolderProgress, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncSnapshot:
    """An immutable copy of the progress of a sync run, for display."""

    state: RunState
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    accounts: tuple[AccountProgress, ...]

    @property
    def messages_total(self) -> int:
        return sum(f.messages_total for a in self.accounts for f in a.folders)

    @property
    def messages_done(self) -> int:
        return sum(f.messages_done for a in self.accounts for f in a.folders)

    @property
    def bytes_total(self) -> int:
        return sum(f.bytes_total for a in self.accounts for f in a.folders)

    @property
    def bytes_done(self) -> int:
        return sum(f.bytes_done for a in self.accounts for f in a.folders)

    @property
    def errors(self) -> list[str]:
        found = [self.error] if self.error else []
        for account in self.accounts:
            if account.error:
                found.append(f"{account.account}: {account.error}")
            found.extend(
                f"{folder.account}/{folder.folder}: {folder.error}"
                for folder in account.folders
                if folder.error
            )
        return found


class SyncProgress:
    """The progress of the current (or the last) sync run. Safe to read from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: RunState = "idle"
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._error: str | None = None
        self._account_errors: dict[str, str | None] = {}
        self._folders: dict[tuple[str, str], FolderProgress] = {}

    def snapshot(self) -> SyncSnapshot:
        with self._lock:
            accounts = tuple(
                AccountProgress(
                    account,
                    error,
                    tuple(p for (owner, _name), p in self._folders.items() if owner == account),
                )
                for account, error in self._account_errors.items()
            )
            return SyncSnapshot(
                self._state, self._started_at, self._finished_at, self._error, accounts
            )

    # The methods below are for the sync engine.

    def start(self, now: datetime) -> None:
        with self._lock:
            self._state = "running"
            self._started_at = now
            self._finished_at = None
            self._error = None
            self._account_errors = {}
            self._folders = {}

    def finish(self, now: datetime, error: str | None = None) -> None:
        with self._lock:
            self._state = "idle"
            self._finished_at = now
            self._error = error

    def add_account(self, account: str) -> None:
        with self._lock:
            self._account_errors.setdefault(account, None)

    def account_failed(self, account: str, error: str) -> None:
        with self._lock:
            self._account_errors[account] = error

    def add_folder(self, account: str, folder: str, state: FolderSyncState = "pending") -> None:
        with self._lock:
            self._account_errors.setdefault(account, None)
            self._folders[(account, folder)] = FolderProgress(account, folder, state)

    def update(
        self,
        account: str,
        folder: str,
        *,
        state: FolderSyncState | None = None,
        messages_total: int | None = None,
        bytes_total: int | None = None,
        messages_done: int = 0,
        bytes_done: int = 0,
        error: str | None = None,
    ) -> None:
        """Change the progress of a folder. The ``*_done`` values are increments."""
        with self._lock:
            current = self._folders.get((account, folder)) or FolderProgress(account, folder)
            self._folders[(account, folder)] = replace(
                current,
                state=state or current.state,
                messages_total=(
                    current.messages_total if messages_total is None else messages_total
                ),
                bytes_total=current.bytes_total if bytes_total is None else bytes_total,
                messages_done=current.messages_done + messages_done,
                bytes_done=current.bytes_done + bytes_done,
                error=error if error is not None else current.error,
            )


def months_before(day: date, months: int) -> date:
    """The same day of the month ``months`` earlier, or the last day of that month."""
    index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(index, 12)
    for candidate in range(day.day, 0, -1):
        try:
            return date(year, month + 1, candidate)
        except ValueError:
            continue
    raise AssertionError("unreachable: every month has a first day")


def plan_batches(
    messages: Sequence[MessageMeta], max_messages: int, max_bytes: int
) -> Iterator[list[MessageMeta]]:
    """Divide messages into batches bounded by count and by total size.

    A message that alone exceeds ``max_bytes`` gets a batch to itself.
    """
    batch: list[MessageMeta] = []
    size = 0
    for message in messages:
        if batch and (len(batch) >= max_messages or size + message.size > max_bytes):
            yield batch
            batch, size = [], 0
        batch.append(message)
        size += message.size
    if batch:
        yield batch


class _FolderChangedError(Exception):
    """The folder changed identity while it was being synced; the next run starts it afresh."""


@dataclass(slots=True)
class _FolderPlan:
    uid_validity: int
    uid_next: int
    wanted: dict[int, MessageMeta] = field(default_factory=dict)


class SyncEngine:
    """Synchronises the cache with the server through the gateway.

    One engine runs one sync at a time (:meth:`sync_all` returns immediately if a run is in
    progress on another thread). Progress can be watched through :attr:`progress`.
    """

    def __init__(
        self,
        gateway: GatewayClient,
        store: Store,
        config: SyncConfig | None = None,
        *,
        progress: SyncProgress | None = None,
        clock: Callable[[], datetime] | None = None,
        parser: Callable[[bytes], ParsedMessage] = parse_message,
    ):
        self.gateway = gateway
        self.store = store
        self.config = config or SyncConfig()
        self.progress = progress or SyncProgress()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._parser = parser
        self._running = threading.Lock()

    # -- entry points --------------------------------------------------------------------

    def sync_all(self) -> SyncSnapshot:
        """Sync every account. Failures are recorded in the progress, not raised."""
        if not self._running.acquire(blocking=False):
            return self.progress.snapshot()
        try:
            self.progress.start(self._clock())
            error: str | None = None
            try:
                self._run()
            except GatewayError as exc:
                error = str(exc)
                logger.warning("sync failed: %s", exc)
            except Exception as exc:
                error = f"internal error: {type(exc).__name__}: {exc}"
                logger.exception("sync failed")
            self.progress.finish(self._clock(), error)
            return self.progress.snapshot()
        finally:
            self._running.release()

    def _run(self) -> None:
        self.store.check_consistency()
        accounts = self.gateway.accounts()
        known = {account.id for account in accounts}
        for cached in self.store.accounts():
            if cached not in known:
                # The user removed the account: its mail must not linger in the cache.
                self.store.remove_account(cached)
        for account in accounts:
            self.progress.add_account(account.id)
        for account in accounts:
            try:
                self._sync_account(account)
            except GatewayError as exc:
                logger.warning("sync of account %s failed: %s", account.id, exc)
                self.progress.account_failed(account.id, str(exc))
            except Exception as exc:
                logger.exception("sync of account %s failed", account.id)
                self.progress.account_failed(
                    account.id, f"internal error: {type(exc).__name__}: {exc}"
                )

    # -- accounts ------------------------------------------------------------------------

    def _sync_account(self, account: Account) -> None:
        folders = self.gateway.list_folders(account.id)
        names = {folder.name for folder in folders}
        for cached in self.store.folders(account.id):
            if cached.name not in names:
                self.store.remove_folder(account.id, cached.name)
        for folder in folders:
            self.store.upsert_folder(
                account.id,
                folder.name,
                role=folder.role,
                delimiter=folder.delimiter,
                selectable=folder.selectable,
                protected=folder.protected,
            )

        to_sync = sorted(
            (f for f in folders if f.selectable and f.role not in SKIPPED_ROLES),
            key=lambda f: (_ROLE_ORDER.get(f.role, 3), f.name),
        )
        for folder in folders:
            if folder not in to_sync:
                if folder.selectable:
                    self.progress.add_folder(account.id, folder.name, "skipped")
                self._forget_messages(account.id, folder.name)
        for folder in to_sync:
            self.progress.add_folder(account.id, folder.name)
        for folder in to_sync:
            self._sync_folder_safely(account.id, folder)

    def _forget_messages(self, account: str, folder: str) -> None:
        state = self.store.folder(account, folder)
        if state is not None and state.uid_validity is not None:
            cached = self.store.cached_flags(account, folder)
            if cached:
                self.store.remove_messages(account, folder, state.uid_validity, cached)

    def _sync_folder_safely(self, account: str, folder: Folder) -> None:
        try:
            self._sync_folder(account, folder)
            self.progress.update(account, folder.name, state="done")
        except _FolderChangedError as exc:
            self.progress.update(account, folder.name, state="error", error=str(exc))
        except (GatewayError, StoreError) as exc:
            logger.warning("sync of %s/%s failed: %s", account, folder.name, exc)
            self.progress.update(account, folder.name, state="error", error=str(exc))
        except Exception as exc:
            logger.exception("sync of %s/%s failed", account, folder.name)
            self.progress.update(
                account,
                folder.name,
                state="error",
                error=f"internal error: {type(exc).__name__}: {exc}",
            )

    # -- folders -------------------------------------------------------------------------

    def _sync_folder(self, account: str, folder: Folder) -> None:
        name = folder.name
        self.progress.update(account, name, state="listing")
        plan = self._plan(account, folder)

        state = self.store.folder(account, name)
        if state is None or state.uid_validity != plan.uid_validity:
            # First sight of the folder, or the server has renumbered it: start afresh.
            self.store.reset_folder(account, name, plan.uid_validity)
        cached = self.store.cached_flags(account, name)

        gone = [uid for uid in cached if uid not in plan.wanted]
        if gone:
            self.store.remove_messages(account, name, plan.uid_validity, gone)
        changed = {
            uid: meta.flags
            for uid, meta in plan.wanted.items()
            if uid in cached and set(cached[uid]) != set(meta.flags)
        }
        if changed:
            self.store.update_flags(account, name, plan.uid_validity, changed)

        # Newest first, so that recent mail becomes available early in a long first sync.
        missing = sorted(
            (meta for uid, meta in plan.wanted.items() if uid not in cached),
            key=lambda meta: meta.uid,
            reverse=True,
        )
        self.progress.update(
            account,
            name,
            state="fetching",
            messages_total=len(missing),
            bytes_total=sum(meta.size for meta in missing),
        )
        for batch in plan_batches(missing, self.config.batch_messages, self.config.batch_bytes):
            self._fetch_batch(account, name, plan.uid_validity, batch)

        self.store.mark_folder_synced(
            account, name, plan.uid_validity, uid_next=plan.uid_next, when=self._clock()
        )

    def _plan(self, account: str, folder: Folder) -> _FolderPlan:
        """Find out which messages of the folder should be in the cache."""
        status = self.gateway.folder_status(account, folder.name)
        plan = _FolderPlan(status.uid_validity, status.uid_next)

        uid_min = 1
        selected: set[int] | None = None
        if folder.role != "inbox":
            cutoff = months_before(self._clock().date(), self.config.months)
            found = self.gateway.search(account, folder.name, q_since(cutoff))
            self._check_validity(plan, found.uid_validity)
            if not found.uids:
                return plan
            selected = set(found.uids)
            uid_min = min(selected)

        for page in self.gateway.iter_message_pages(
            account, folder.name, uid_min=uid_min, page_size=self.config.page_size
        ):
            self._check_validity(plan, page.uid_validity)
            for meta in page.messages:
                if selected is None or meta.uid in selected:
                    plan.wanted[meta.uid] = meta
        return plan

    @staticmethod
    def _check_validity(plan: _FolderPlan, uid_validity: int | None) -> None:
        if uid_validity != plan.uid_validity:
            raise _FolderChangedError("the folder changed on the server during the sync")

    def _fetch_batch(
        self, account: str, folder: str, uid_validity: int, batch: Sequence[MessageMeta]
    ) -> None:
        expected = {meta.uid: meta for meta in batch}
        received: set[int] = set()
        try:
            with self.gateway.fetch_messages(account, folder, list(expected)) as stream:
                for message in stream:
                    if message.uid not in expected or message.uid in received:
                        raise GatewayProtocolError(
                            f"the gateway sent message {message.uid}, which was not asked for"
                        )
                    received.add(message.uid)
                    self._store_message(account, folder, uid_validity, message)
                    self.progress.update(
                        account, folder, messages_done=1, bytes_done=expected[message.uid].size
                    )
                final_validity = stream.uid_validity
        except UidValidityChangedError as exc:
            raise _FolderChangedError(str(exc)) from exc
        if final_validity != uid_validity:
            # What was just stored may belong to different messages than we believe. Drop the
            # folder now; the next run downloads it again under its new identity.
            if final_validity is not None:
                self.store.reset_folder(account, folder, final_validity)
            raise _FolderChangedError("the folder changed on the server during the sync")
        # Messages that vanished between listing and fetching are simply no longer wanted.
        for uid, meta in expected.items():
            if uid not in received:
                self.progress.update(account, folder, messages_done=1, bytes_done=meta.size)

    def _store_message(
        self, account: str, folder: str, uid_validity: int, message: FetchedMessage
    ) -> None:
        parsed: ParsedMessage | None
        parse_error: str | None = None
        try:
            parsed = self._parser(message.raw)
        except Exception as exc:
            # The parser promises not to raise, but a message that breaks that promise must
            # not stop the sync: keep the raw message and note what happened.
            logger.warning("could not parse message %s in %s/%s", message.uid, account, folder)
            parsed = None
            parse_error = f"{type(exc).__name__}: {exc}"[:500]
        if parsed is not None and parsed.problems:
            parse_error = "; ".join(parsed.problems)[:500]
        new = NewMessage(
            uid=message.uid,
            flags=message.flags,
            internal_date=message.internal_date,
            raw=message.raw,
            parsed=parsed,
            parse_error=parse_error,
        )
        try:
            self.store.add_messages(account, folder, uid_validity, [new])
        except StaleFolderError as exc:
            raise _FolderChangedError(str(exc)) from exc

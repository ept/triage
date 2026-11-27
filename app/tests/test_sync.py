from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from triage.gateway import GatewayClient, MessageMeta, Recipient
from triage.mime import ParsedMessage
from triage.store import MessageKey, Store
from triage.sync import SyncConfig, SyncEngine, SyncSnapshot, months_before, plan_batches
from triage.testing.fake_gateway import FakeGateway, Fault
from triage.testing.samples import ALEX, KIM, USER, make_message

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(client: GatewayClient, store: Store) -> SyncEngine:
    return SyncEngine(client, store, clock=lambda: NOW)


def deliver(
    fake: FakeGateway,
    folder: str = "INBOX",
    *,
    age_days: float = 1,
    account: str = "work",
    flags=(),
    **kwargs,
) -> int:
    when = NOW - timedelta(days=age_days)
    kwargs.setdefault("date", when)
    return fake.add_message(
        account, folder, make_message(**kwargs), flags=flags, internal_date=when
    )


def folder_states(snapshot: SyncSnapshot) -> dict[tuple[str, str], str]:
    return {(f.account, f.folder): f.state for a in snapshot.accounts for f in a.folders}


def cached(store: Store, folder: str = "INBOX", account: str = "work") -> list[int]:
    return sorted(store.cached_flags(account, folder))


def key(fake: FakeGateway, uid: int, folder: str = "INBOX", account: str = "work") -> MessageKey:
    return MessageKey(account, folder, fake.folder(account, folder).uid_validity, uid)


# -- helpers ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2026, 11, 20), 3, date(2026, 8, 20)),
        (date(2026, 2, 10), 3, date(2025, 11, 10)),
        (date(2026, 1, 31), 1, date(2025, 12, 31)),
        (date(2026, 3, 31), 1, date(2026, 2, 28)),
        (date(2024, 3, 31), 1, date(2024, 2, 29)),
        (date(2026, 5, 31), 3, date(2026, 2, 28)),
        (date(2026, 12, 31), 12, date(2025, 12, 31)),
        (date(2026, 7, 31), 0, date(2026, 7, 31)),
        (date(2026, 7, 31), 25, date(2024, 6, 30)),
    ],
)
def test_months_before(day: date, months: int, expected: date) -> None:
    assert months_before(day, months) == expected


def test_plan_batches() -> None:
    def metas(*sizes: int) -> list[MessageMeta]:
        return [MessageMeta(uid, (), size, NOW) for uid, size in enumerate(sizes, 1)]

    def plan(sizes: tuple[int, ...], max_messages: int, max_bytes: int) -> list[list[int]]:
        return [
            [m.size for m in batch]
            for batch in plan_batches(metas(*sizes), max_messages, max_bytes)
        ]

    assert plan((), 3, 100) == []
    assert plan((10, 10, 10, 10), 3, 100) == [[10, 10, 10], [10]]
    assert plan((60, 30, 20, 50, 50), 10, 100) == [[60, 30], [20, 50], [50]]
    # A message bigger than the limit travels alone rather than not at all.
    assert plan((10, 500, 10, 10), 10, 100) == [[10], [500], [10, 10]]
    assert plan((500,), 10, 100) == [[500]]
    assert plan((100, 1), 10, 100) == [[100], [1]]


# -- what gets synced ------------------------------------------------------------------------


def test_first_sync_fills_the_cache(fake: FakeGateway, store: Store, engine: SyncEngine) -> None:
    deliver(fake, subject="Budget", sender=ALEX, flags={"\\Seen"})
    deliver(fake, subject="Lunch", sender=KIM, html="<p>Lunch?</p>")
    deliver(fake, "Archive", subject="Old plans", age_days=30)

    snapshot = engine.sync_all()

    assert snapshot.state == "idle"
    assert snapshot.errors == []
    assert (snapshot.messages_done, snapshot.messages_total) == (3, 3)
    assert snapshot.bytes_done == snapshot.bytes_total > 0
    assert snapshot.started_at == snapshot.finished_at == NOW
    assert set(folder_states(snapshot).values()) == {"done"}
    assert set(folder_states(snapshot)) == {
        ("work", name) for name in ("INBOX", "Drafts", "Sent", "Archive", "Junk", "Trash")
    }

    assert cached(store) == [1, 2]
    message = store.get_message(key(fake, 1))
    assert message is not None
    assert (message.subject, message.flags) == ("Budget", ("\\Seen",))
    assert message.internal_date == NOW - timedelta(days=1)
    assert store.get_raw(key(fake, 2)) == fake.folder("work", "INBOX").messages[2].raw
    assert [m.key.folder for m in store.search("plans")] == ["Archive"]

    state = store.folder("work", "INBOX")
    assert state is not None
    assert (state.role, state.uid_next, state.synced_at) == ("inbox", 3, NOW)
    assert state.uid_validity == fake.folder("work", "INBOX").uid_validity
    drafts = store.folder("work", "Drafts")
    assert drafts is not None
    assert drafts.protected


def test_second_sync_downloads_nothing(fake: FakeGateway, engine: SyncEngine) -> None:
    for _ in range(3):
        deliver(fake)
    engine.sync_all()
    fake.requests.clear()
    snapshot = engine.sync_all()
    assert snapshot.messages_total == 0
    assert snapshot.errors == []
    assert ("POST", "/v1/messages/fetch") not in fake.requests


def test_inbox_is_complete_but_other_folders_only_recent(
    fake: FakeGateway, store: Store, engine: SyncEngine
) -> None:
    for age in (1, 100, 400, 2000):
        deliver(fake, "INBOX", age_days=age)
        deliver(fake, "Archive", age_days=age)
    # The cutoff is three months before 20 November, i.e. 20 August (92 days).
    deliver(fake, "Archive", age_days=91.9)
    deliver(fake, "Archive", age_days=92.6)
    engine.sync_all()
    assert cached(store, "INBOX") == [1, 2, 3, 4]
    assert cached(store, "Archive") == [1, 5]


def test_configurable_window(fake: FakeGateway, store: Store, client: GatewayClient) -> None:
    for age in (1, 100, 400):
        deliver(fake, "Archive", age_days=age)
    SyncEngine(client, store, SyncConfig(months=12), clock=lambda: NOW).sync_all()
    assert cached(store, "Archive") == [1, 2]
    # Narrowing the window later drops what falls outside it.
    SyncEngine(client, store, SyncConfig(months=1), clock=lambda: NOW).sync_all()
    assert cached(store, "Archive") == [1]


def test_messages_age_out_of_the_window(
    fake: FakeGateway, store: Store, client: GatewayClient
) -> None:
    deliver(fake, "Archive", age_days=80)
    deliver(fake, "INBOX", age_days=80)
    now = NOW
    engine = SyncEngine(client, store, clock=lambda: now)
    engine.sync_all()
    assert cached(store, "Archive") == [1]
    now = NOW + timedelta(days=30)
    engine.sync_all()
    assert cached(store, "Archive") == []
    assert cached(store, "INBOX") == [1]


def test_virtual_and_unselectable_folders_are_skipped(
    fake: FakeGateway, store: Store, engine: SyncEngine
) -> None:
    fake.add_folder("work", "All Mail", role="all")
    fake.add_folder("work", "Starred", role="flagged")
    fake.add_folder("work", "Parent", selectable=False)
    fake.add_folder("work", "Parent/Child")
    for folder in ("All Mail", "Starred", "Parent/Child"):
        deliver(fake, folder)
    snapshot = engine.sync_all()
    states = folder_states(snapshot)
    assert states[("work", "All Mail")] == "skipped"
    assert states[("work", "Starred")] == "skipped"
    assert ("work", "Parent") not in states
    assert states[("work", "Parent/Child")] == "done"
    assert cached(store, "All Mail") == []
    assert cached(store, "Parent/Child") == [1]
    # The folders are still known, so that the UI can show them.
    assert {f.name for f in store.folders("work")} >= {"All Mail", "Starred", "Parent"}
    assert ("POST", "/v1/folders/status") in fake.requests


def test_folder_that_becomes_virtual_is_emptied(
    fake: FakeGateway, store: Store, engine: SyncEngine
) -> None:
    fake.add_folder("work", "Everything")
    deliver(fake, "Everything")
    engine.sync_all()
    assert cached(store, "Everything") == [1]
    fake.folder("work", "Everything").role = "all"
    engine.sync_all()
    assert cached(store, "Everything") == []


def test_inbox_is_synced_first_and_newest_messages_first(
    fake: FakeGateway, client: GatewayClient, store: Store
) -> None:
    for folder in ("Trash", "Archive", "Sent", "INBOX"):
        for _ in range(3):
            deliver(fake, folder)
    order: list[tuple[str, int]] = []
    original = store.add_messages

    def recording(account, folder, uid_validity, messages):
        messages = list(messages)
        order.extend((folder, m.uid) for m in messages)
        return original(account, folder, uid_validity, messages)

    store.add_messages = recording  # type: ignore[method-assign]
    SyncEngine(client, store, clock=lambda: NOW).sync_all()
    assert [folder for folder, _ in order[::3]] == ["INBOX", "Sent", "Archive", "Trash"]
    assert [uid for folder, uid in order if folder == "INBOX"] == [3, 2, 1]


# -- incremental behaviour -------------------------------------------------------------------


def test_changes_on_the_server_are_reflected(
    fake: FakeGateway, store: Store, engine: SyncEngine
) -> None:
    for subject in ("one", "two", "three", "four"):
        deliver(fake, subject=subject)
    engine.sync_all()

    fake.set_flags_directly("work", "INBOX", 1, {"\\Seen", "\\Answered"})  # read and answered
    fake.delete_message("work", "INBOX", 2)  # expunged by another client
    new_uid = fake.move_directly("work", "INBOX", 3, "Archive")  # filed by the user
    deliver(fake, subject="five")  # newly arrived
    fake.requests.clear()

    snapshot = engine.sync_all()
    assert snapshot.errors == []
    assert snapshot.messages_total == 2  # only the new message and the moved one are fetched
    assert cached(store) == [1, 4, 5]
    assert store.cached_flags("work", "INBOX")[1] == ("\\Answered", "\\Seen")
    assert cached(store, "Archive") == [new_uid]
    moved = store.get_message(key(fake, new_uid, "Archive"))
    assert moved is not None
    assert moved.subject == "three"
    assert [m.subject for m in store.search("two")] == []
    assert [m.subject for m in store.list_folder("work", "INBOX")] == ["five", "four", "one"]
    assert store.check_consistency() == (0, 1)  # the raw copy of the expunged message


def test_changes_made_through_the_gateway_are_reflected(
    fake: FakeGateway, client: GatewayClient, store: Store, engine: SyncEngine
) -> None:
    deliver(fake, subject="to archive")
    deliver(fake, subject="to mark")
    engine.sync_all()
    validity = fake.folder("work", "INBOX").uid_validity
    client.move_messages("work", "INBOX", validity, [1], "Archive")
    client.set_flags("work", "INBOX", validity, [2], add=["\\Seen", "$Triaged"])
    engine.sync_all()
    assert cached(store) == [2]
    assert store.cached_flags("work", "INBOX")[2] == ("$Triaged", "\\Seen")
    assert [m.subject for m in store.list_folder("work", "Archive")] == ["to archive"]


def test_uid_validity_change_resyncs_the_folder(
    fake: FakeGateway, store: Store, engine: SyncEngine
) -> None:
    for subject in ("one", "two", "three"):
        deliver(fake, subject=subject)
    deliver(fake, "Archive", subject="untouched")
    engine.sync_all()
    old_key = key(fake, 3)
    store.set_analysis(old_key, "v1", {"summary": "kept"})
    fake.delete_message("work", "INBOX", 1)
    fake.reset_uid_validity("work", "INBOX")  # "two" is now UID 1, "three" is UID 2
    fake.requests.clear()

    snapshot = engine.sync_all()
    assert snapshot.errors == []
    assert store.get_message(old_key) is None
    assert cached(store) == [1, 2]
    renumbered = store.get_message(key(fake, 2))
    assert renumbered is not None
    assert renumbered.subject == "three"
    state = store.folder("work", "INBOX")
    assert state is not None
    assert state.uid_validity == fake.folder("work", "INBOX").uid_validity
    # Only the affected folder was downloaded again, and the analysis survived the renumbering.
    assert snapshot.messages_total == 2
    analysis = store.get_analysis(key(fake, 2))
    assert analysis is not None
    assert analysis.result == {"summary": "kept"}


def test_removed_folders_and_accounts_are_forgotten(
    fake: FakeGateway, store: Store, engine: SyncEngine
) -> None:
    fake.add_account("home", "robin@home.example")
    fake.add_folder("work", "Project")
    deliver(fake, "Project", subject="project mail")
    deliver(fake, account="home", subject="private mail")
    engine.sync_all()
    assert store.accounts() == ["home", "work"]
    assert len(store.search("mail")) == 2

    fake.remove_folder("work", "Project")
    fake.remove_account("home")
    snapshot = engine.sync_all()
    assert snapshot.errors == []
    assert store.accounts() == ["work"]
    assert store.folder("work", "Project") is None
    assert store.search("mail") == []
    engine.sync_all()
    assert store.stats().raw_messages == 0  # and the content itself is gone too


# -- batching --------------------------------------------------------------------------------


def test_batches_are_bounded_by_count_and_size(
    fake: FakeGateway, client: GatewayClient, store: Store
) -> None:
    sizes = [2_000, 2_000, 30_000, 2_000, 2_000, 2_000, 2_000]
    for size in sizes:
        deliver(fake, text="x" * size)
    batches: list[list[int]] = []
    original = client.fetch_messages

    def recording(account, folder, uids):
        batches.append(list(uids))
        return original(account, folder, uids)

    client.fetch_messages = recording  # type: ignore[method-assign]
    config = SyncConfig(batch_messages=3, batch_bytes=10_000)
    snapshot = SyncEngine(client, store, config, clock=lambda: NOW).sync_all()
    assert snapshot.errors == []
    # Newest first; three at most; the large message alone.
    assert batches == [[7, 6, 5], [4], [3], [2, 1]]
    assert cached(store) == [1, 2, 3, 4, 5, 6, 7]


def test_listing_is_paged(fake: FakeGateway, client: GatewayClient, store: Store) -> None:
    for _ in range(25):
        deliver(fake)
        deliver(fake, "Archive")
    snapshot = SyncEngine(client, store, SyncConfig(page_size=10), clock=lambda: NOW).sync_all()
    assert snapshot.errors == []
    assert cached(store) == list(range(1, 26))
    assert cached(store, "Archive") == list(range(1, 26))
    assert fake.requests.count(("POST", "/v1/messages/list")) >= 6


# -- failures --------------------------------------------------------------------------------


def test_one_failing_folder_does_not_stop_the_others(
    fake: FakeGateway, store: Store, engine: SyncEngine
) -> None:
    deliver(fake, "INBOX")
    deliver(fake, "Sent")
    deliver(fake, "Archive")
    fake.add_fault(Fault(code="imap_error", message="mailbox is locked", folder="Sent"))
    snapshot = engine.sync_all()
    states = folder_states(snapshot)
    assert states[("work", "Sent")] == "error"
    assert states[("work", "INBOX")] == states[("work", "Archive")] == "done"
    assert snapshot.errors == ["work/Sent: imap_error: mailbox is locked"]
    assert cached(store) == [1]
    assert cached(store, "Archive") == [1]
    assert cached(store, "Sent") == []

    fake.clear_faults()
    assert engine.sync_all().errors == []
    assert cached(store, "Sent") == [1]


def test_one_failing_account_does_not_stop_the_others(
    fake: FakeGateway, store: Store, engine: SyncEngine
) -> None:
    fake.add_account("aaa", "first@example.org")
    fake.add_account("zzz", "last@example.org")
    for account in ("aaa", "work", "zzz"):
        deliver(fake, account=account)
    fake.add_fault(
        Fault(code="unavailable", message="login failed", account="work", op="folders/list")
    )
    snapshot = engine.sync_all()
    assert snapshot.errors == ["work: unavailable: login failed"]
    assert {a.account: a.error for a in snapshot.accounts} == {
        "work": "unavailable: login failed",
        "aaa": None,
        "zzz": None,
    }
    assert cached(store, account="aaa") == [1]
    assert cached(store, account="zzz") == [1]


def test_total_failure_is_reported_not_raised(
    fake: FakeGateway, store: Store, engine: SyncEngine
) -> None:
    deliver(fake)
    engine.sync_all()
    fake.add_fault(Fault(code="unavailable", message="gateway is starting", op="accounts"))
    snapshot = engine.sync_all()
    assert snapshot.state == "idle"
    assert snapshot.error == "unavailable: gateway is starting"
    assert snapshot.errors == ["unavailable: gateway is starting"]
    assert cached(store) == [1]  # a failed run must not be mistaken for "no accounts"


def test_interrupted_download_resumes(
    fake: FakeGateway, client: GatewayClient, store: Store
) -> None:
    for _ in range(10):
        deliver(fake)
    engine = SyncEngine(client, store, SyncConfig(batch_messages=4), clock=lambda: NOW)

    fake.truncate_fetch_after = 3  # every stream breaks after three messages
    snapshot = engine.sync_all()
    assert folder_states(snapshot)[("work", "INBOX")] == "error"
    assert "truncated_stream" in snapshot.errors[0]
    assert cached(store) == [8, 9, 10]  # what arrived before the break was kept
    progress = next(f for a in snapshot.accounts for f in a.folders if f.folder == "INBOX")
    assert (progress.messages_done, progress.messages_total) == (3, 10)

    fake.clear_faults()
    fake.requests.clear()
    snapshot = engine.sync_all()
    assert snapshot.errors == []
    assert snapshot.messages_total == 7  # only the remainder
    assert cached(store) == list(range(1, 11))


def test_error_frame_in_the_stream(fake: FakeGateway, store: Store, engine: SyncEngine) -> None:
    for _ in range(3):
        deliver(fake)
    fake.fetch_error_after = (2, "unavailable")
    snapshot = engine.sync_all()
    assert snapshot.errors == ["work/INBOX: unavailable: injected stream error"]
    assert cached(store) == [2, 3]


def test_uid_validity_change_in_the_middle_of_a_sync(
    fake: FakeGateway, client: GatewayClient, store: Store
) -> None:
    for subject in ("one", "two", "three"):
        deliver(fake, subject=subject)
    engine = SyncEngine(client, store, clock=lambda: NOW)
    original = client.fetch_messages

    def renumber_first(account, folder, uids):
        if folder == "INBOX":
            fake.delete_message("work", "INBOX", 1)
            fake.reset_uid_validity("work", "INBOX")
            client.fetch_messages = original  # type: ignore[method-assign]
        return original(account, folder, uids)

    client.fetch_messages = renumber_first  # type: ignore[method-assign]
    snapshot = engine.sync_all()
    assert folder_states(snapshot)[("work", "INBOX")] == "error"
    # Nothing that was fetched under the old numbering may remain in the cache.
    assert cached(store) == []

    snapshot = engine.sync_all()
    assert snapshot.errors == []
    assert [m.subject for m in store.list_folder("work", "INBOX", newest_first=False)] == [
        "two",
        "three",
    ]


def test_message_that_vanishes_between_listing_and_fetching(
    fake: FakeGateway, client: GatewayClient, store: Store
) -> None:
    for _ in range(3):
        deliver(fake)
    original = client.fetch_messages

    def expunge_first(account, folder, uids):
        if 2 in fake.folder("work", "INBOX").messages:
            fake.delete_message("work", "INBOX", 2)
        return original(account, folder, uids)

    client.fetch_messages = expunge_first  # type: ignore[method-assign]
    snapshot = SyncEngine(client, store, clock=lambda: NOW).sync_all()
    assert snapshot.errors == []
    assert cached(store) == [1, 3]
    assert (snapshot.messages_done, snapshot.messages_total) == (3, 3)


def test_parser_failure_keeps_the_raw_message(
    fake: FakeGateway, client: GatewayClient, store: Store
) -> None:
    deliver(fake, subject="fine")
    deliver(fake, subject="breaks the parser")
    deliver(fake, subject="has problems")

    def parser(raw: bytes) -> ParsedMessage:
        if b"breaks the parser" in raw:
            raise ValueError("unexpected")
        if b"has problems" in raw:
            return ParsedMessage(subject="has problems", problems=["odd structure", "odd charset"])
        return ParsedMessage(subject="fine")

    snapshot = SyncEngine(client, store, clock=lambda: NOW, parser=parser).sync_all()
    assert snapshot.errors == []
    assert cached(store) == [1, 2, 3]
    broken = store.get_message(key(fake, 2))
    assert broken is not None
    assert (broken.subject, broken.parse_error) == ("", "ValueError: unexpected")
    assert store.get_raw(key(fake, 2)) == fake.folder("work", "INBOX").messages[2].raw
    odd = store.get_message(key(fake, 3))
    assert odd is not None
    assert odd.parse_error == "odd structure; odd charset"
    fine = store.get_message(key(fake, 1))
    assert fine is not None
    assert fine.parse_error is None


def test_gateway_sending_unrequested_messages_is_an_error(
    fake: FakeGateway, client: GatewayClient, store: Store
) -> None:
    deliver(fake)
    deliver(fake)
    original = client.fetch_messages
    client.fetch_messages = lambda account, folder, uids: original(account, folder, [1, 2])  # type: ignore[method-assign]
    engine = SyncEngine(client, store, SyncConfig(batch_messages=1), clock=lambda: NOW)
    snapshot = engine.sync_all()
    assert "protocol_error" in snapshot.errors[0]


def test_internal_errors_are_contained(
    fake: FakeGateway, client: GatewayClient, store: Store
) -> None:
    deliver(fake)
    deliver(fake, "Archive")
    engine = SyncEngine(client, store, clock=lambda: NOW)
    original = store.cached_flags

    def broken(account, folder):
        if folder == "INBOX":
            raise RuntimeError("bug")
        return original(account, folder)

    store.cached_flags = broken  # type: ignore[method-assign]
    snapshot = engine.sync_all()
    assert snapshot.errors == ["work/INBOX: internal error: RuntimeError: bug"]
    assert cached(store, "Archive") == [1]


# -- progress and concurrency ----------------------------------------------------------------


def test_progress_can_be_watched_while_syncing(
    fake: FakeGateway, client: GatewayClient, store: Store
) -> None:
    for _ in range(6):
        deliver(fake)
    engine = SyncEngine(client, store, SyncConfig(batch_messages=2), clock=lambda: NOW)
    assert engine.progress.snapshot().state == "idle"
    assert engine.progress.snapshot().started_at is None
    observed: list[SyncSnapshot] = []
    original = client.fetch_messages

    def observing(account, folder, uids):
        observed.append(engine.progress.snapshot())
        return original(account, folder, uids)

    client.fetch_messages = observing  # type: ignore[method-assign]
    engine.sync_all()
    assert [s.state for s in observed] == ["running"] * 3
    assert [s.messages_done for s in observed] == [0, 2, 4]
    assert all(s.messages_total == 6 for s in observed)
    assert [folder_states(s)[("work", "INBOX")] for s in observed] == ["fetching"] * 3
    assert folder_states(observed[0])[("work", "Archive")] == "pending"
    assert observed[1].bytes_done > 0
    final = engine.progress.snapshot()
    assert (final.state, final.messages_done) == ("idle", 6)


def test_only_one_sync_runs_at_a_time(
    fake: FakeGateway, client: GatewayClient, store: Store
) -> None:
    deliver(fake)
    engine = SyncEngine(client, store, clock=lambda: NOW)
    entered = threading.Event()
    release = threading.Event()
    original = client.accounts

    def slow_accounts():
        entered.set()
        assert release.wait(30)
        return original()

    client.accounts = slow_accounts  # type: ignore[method-assign]
    thread = threading.Thread(target=engine.sync_all)
    thread.start()
    assert entered.wait(30)
    second = engine.sync_all()  # returns at once with the progress of the run in flight
    assert second.state == "running"
    release.set()
    thread.join(30)
    assert engine.progress.snapshot().state == "idle"
    assert fake.requests.count(("GET", "/v1/accounts")) == 1  # the second call did not sync
    assert cached(store) == [1]


# -- end to end ------------------------------------------------------------------------------


def test_end_to_end(fake: FakeGateway, client: GatewayClient, store: Store) -> None:
    """Synthetic mail -> sync -> search -> changes on the server -> resync."""
    fake.add_account("home", "robin@home.example", "Robin")
    fake.add_folder("work", "Projects/Lighthouse")
    first = deliver(
        fake,
        age_days=6,
        sender=KIM,
        subject="Lighthouse project: draft schedule",
        message_id="<lh1@example.net>",
        text="Hi Robin,\n\ncould you check the dates?\n\nKim\n",
        flags={"\\Seen"},
    )
    deliver(
        fake,
        "Sent",
        age_days=5,
        sender=USER,
        to=[KIM],
        subject="Re: Lighthouse project: draft schedule",
        message_id="<lh2@example.org>",
        in_reply_to="<lh1@example.net>",
        references=["<lh1@example.net>"],
        text="Week two clashes with the workshop.\n\n> could you check the dates?\n",
        flags={"\\Seen"},
    )
    third = deliver(
        fake,
        age_days=2,
        sender=KIM,
        subject="Re: Lighthouse project: draft schedule",
        message_id="<lh3@example.net>",
        in_reply_to="<lh2@example.org>",
        references=["<lh1@example.net>", "<lh2@example.org>"],
        text="Can you confirm by Friday whether week three works?\n",
    )
    newsletter = deliver(
        fake,
        age_days=1,
        sender=("Example Weekly", "news@newsletter.example"),
        subject="Größere Neuigkeiten",
        text=None,
        html='<h1>Neuigkeiten</h1><p>Zehn neue Beispiele.</p><img src="https://newsletter.example/p.gif">',
    )
    invoice = deliver(
        fake,
        age_days=0.2,
        sender=ALEX,
        subject="Invoice 2026-117 due next week",
        text="Please find our invoice attached.\n",
        attachments=[("invoice-2026-117.pdf", "application/pdf", b"%PDF-1.4 synthetic\n")],
    )
    deliver(fake, "Projects/Lighthouse", age_days=200, subject="Ancient kick-off notes")
    deliver(fake, account="home", sender=KIM, subject="Weekend lighthouse trip?")

    engine = SyncEngine(client, store, clock=lambda: NOW)
    snapshot = engine.sync_all()
    assert snapshot.errors == []
    assert snapshot.messages_done == 6  # everything but the ancient message

    # Search across folders, restricted to an account, with filters.
    assert [m.subject for m in store.search("lighthouse", account="work", order="date")] == [
        "Re: Lighthouse project: draft schedule",
        "Re: Lighthouse project: draft schedule",
        "Lighthouse project: draft schedule",
    ]
    assert [m.key.account for m in store.search("lighthouse trip")] == ["home"]
    assert [m.key.uid for m in store.search("", account="work", folder="INBOX", unread=True)] == [
        invoice,
        newsletter,
        third,
    ]
    assert [m.key.uid for m in store.search("neuigkeiten beispiele")] == [newsletter]
    assert [m.key.uid for m in store.search("from:alex", has_attachments=True)] == [invoice]
    assert store.search("ancient") == []

    # The conversation spans the inbox and the sent folder.
    conversation = store.conversation(key(fake, third))
    assert [(m.key.folder, m.message_id) for m in conversation] == [
        ("INBOX", "<lh1@example.net>"),
        ("Sent", "<lh2@example.org>"),
        ("INBOX", "<lh3@example.net>"),
    ]

    # The HTML message is searchable as text and displayable without its tracking pixel.
    parsed = store.get_parsed(key(fake, newsletter))
    assert parsed is not None
    assert parsed.html is not None
    assert parsed.blocked_remote_content == 1
    assert "newsletter.example" not in parsed.html
    stored = store.get_message(key(fake, newsletter))
    assert stored is not None
    assert (stored.subject, stored.body) == (
        "Größere Neuigkeiten",
        "Neuigkeiten\n\nZehn neue Beispiele.",
    )

    store.set_analysis(key(fake, invoice), "v1", {"needs_reply": True})
    assert len(store.unanalysed("v1", account="work")) == 4

    # Meanwhile, on the server: the user reads, files, and deletes; mail arrives; and the
    # app itself drafts a reply and archives a message through the gateway.
    inbox_validity = fake.folder("work", "INBOX").uid_validity
    fake.set_flags_directly("work", "INBOX", third, {"\\Seen", "\\Answered"})
    fake.delete_message("work", "INBOX", newsletter)
    filed = fake.move_directly("work", "INBOX", first, "Projects/Lighthouse")
    arrived = deliver(
        fake, age_days=0, sender=KIM, subject="One more thing", text="Bring the charts.\n"
    )
    moved = client.move_messages("work", "INBOX", inbox_validity, [invoice], "Archive").moved[0]
    client.create_draft(
        "work",
        to=[Recipient(KIM[1], KIM[0])],
        subject="Re: Lighthouse project: draft schedule",
        body="Week three works for me.\n",
        in_reply_to=__import__("triage.gateway", fromlist=["MessageRef"]).MessageRef(
            "INBOX", inbox_validity, third
        ),
    )
    fake.reset_uid_validity("home", "INBOX")

    snapshot = engine.sync_all()
    assert snapshot.errors == []

    assert cached(store, "INBOX") == [third, arrived]
    assert store.cached_flags("work", "INBOX")[third] == ("\\Answered", "\\Seen")
    assert cached(store, "Projects/Lighthouse") == [filed]  # recent, unlike the ancient one
    assert cached(store, "Archive") == [moved.new_uid]
    assert store.search("neuigkeiten") == []
    assert [m.key.folder for m in store.search("invoice")] == ["Archive"]
    assert [m.subject for m in store.search("charts")] == ["One more thing"]

    # The analysis moved with the invoice; the draft is cached and joins the conversation.
    assert moved.new_uid is not None
    analysis = store.get_analysis(key(fake, moved.new_uid, "Archive"))
    assert analysis is not None
    assert analysis.result == {"needs_reply": True}
    drafts = store.list_folder("work", "Drafts")
    assert [(m.subject, m.flags) for m in drafts] == [
        ("Re: Lighthouse project: draft schedule", ("\\Draft", "\\Seen"))
    ]
    assert [m.key.folder for m in store.conversation(key(fake, third))] == [
        "Projects/Lighthouse",
        "Sent",
        "INBOX",
        "Drafts",
    ]

    # The other account was renumbered and is intact under its new identity.
    home = store.list_folder("home", "INBOX")
    assert [m.subject for m in home] == ["Weekend lighthouse trip?"]
    assert home[0].key.uid_validity == fake.folder("home", "INBOX").uid_validity

    # Counts agree with the server, and nothing is left behind in the raw store.
    counts = {(c.account, c.folder): (c.total, c.unread) for c in store.counts()}
    assert counts[("work", "INBOX")] == (2, 1)
    assert counts[("work", "Archive")] == (1, 1)
    engine.sync_all()
    assert store.stats().raw_messages == store.stats().messages == 7
    assert [entry.op for entry in client.log().entries] == ["move", "draft"]

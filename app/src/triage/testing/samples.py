"""Synthetic email messages for tests and demos.

All names and addresses are fictional and use the reserved ``example`` domains.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, formataddr

from triage.testing.fake_gateway import FakeGateway

Person = tuple[str, str]

USER: Person = ("Robin Tester", "robin@example.org")
ALEX: Person = ("Alex Sample", "alex@example.com")
KIM: Person = ("Kim Placeholder", "kim@example.net")
NEWSLETTER: Person = ("Example Weekly", "news@newsletter.example")


def make_message(
    *,
    sender: Person = ALEX,
    to: Sequence[Person] = (USER,),
    cc: Sequence[Person] = (),
    subject: str = "Hello",
    text: str | None = "Hello,\n\nthis is a test message.\n",
    html: str | None = None,
    date: datetime | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: Iterable[str] = (),
    attachments: Sequence[tuple[str, str, bytes]] = (),
    headers: Sequence[tuple[str, str]] = (),
) -> bytes:
    """Build a well-formed message. ``attachments`` are (filename, content type, data)."""
    message = EmailMessage()
    message["From"] = formataddr(sender)
    if to:
        message["To"] = ", ".join(formataddr(p) for p in to)
    if cc:
        message["Cc"] = ", ".join(formataddr(p) for p in cc)
    message["Subject"] = subject
    message["Date"] = format_datetime(date or datetime(2026, 11, 2, 9, 30, tzinfo=UTC))
    if message_id is not None:
        message["Message-ID"] = message_id
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
    references = list(references)
    if references:
        message["References"] = " ".join(references)
    for name, value in headers:
        message[name] = value
    if text is not None:
        message.set_content(text)
        if html is not None:
            message.add_alternative(html, subtype="html")
    elif html is not None:
        message.set_content(html, subtype="html")
    for filename, content_type, data in attachments:
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes(policy=SMTP)


def demo_gateway(now: datetime | None = None) -> FakeGateway:
    """A fake gateway with one account and a handful of synthetic messages."""
    now = now or datetime.now(UTC)
    fake = FakeGateway()
    fake.add_account("demo", USER[1], USER[0])
    fake.add_folder("demo", "Projects/Lighthouse")

    def deliver(folder: str, age: timedelta, flags: Iterable[str] = (), **kwargs: object) -> int:
        when = now - age
        raw = make_message(date=when, **kwargs)  # type: ignore[arg-type]
        return fake.add_message("demo", folder, raw, flags=flags, internal_date=when)

    deliver(
        "INBOX",
        timedelta(days=6),
        flags={"\\Seen"},
        sender=KIM,
        subject="Lighthouse project: draft schedule",
        message_id="<lighthouse-1@example.net>",
        text="Hi Robin,\n\nattached is the draft schedule. Could you check the dates?\n\nKim\n",
        attachments=[("schedule.txt", "text/plain", b"Week 1: planning\nWeek 2: building\n")],
    )
    deliver(
        "Sent",
        timedelta(days=5, hours=20),
        flags={"\\Seen"},
        sender=USER,
        to=[KIM],
        subject="Re: Lighthouse project: draft schedule",
        message_id="<lighthouse-2@example.org>",
        in_reply_to="<lighthouse-1@example.net>",
        references=["<lighthouse-1@example.net>"],
        text=(
            "Hi Kim,\n\nlooks good, but week 2 clashes with the workshop.\n\nRobin\n\n"
            "On Mon, 2 Nov 2026 at 09:30, Kim Placeholder <kim@example.net> wrote:\n"
            "> Hi Robin,\n>\n> attached is the draft schedule. Could you check the dates?\n"
        ),
    )
    deliver(
        "INBOX",
        timedelta(days=2),
        sender=KIM,
        subject="Re: Lighthouse project: draft schedule",
        message_id="<lighthouse-3@example.net>",
        in_reply_to="<lighthouse-2@example.org>",
        references=["<lighthouse-1@example.net>", "<lighthouse-2@example.org>"],
        text=(
            "Good point. Can you confirm by Friday whether week 3 works instead?\n\nKim\n\n"
            "On Tue, 3 Nov 2026 at 13:10, Robin Tester <robin@example.org> wrote:\n"
            "> looks good, but week 2 clashes with the workshop.\n"
        ),
    )
    deliver(
        "INBOX",
        timedelta(days=1),
        sender=NEWSLETTER,
        subject="This week in examples",
        message_id="<weekly-42@newsletter.example>",
        text="This week: ten new examples.\n\nRead more at https://newsletter.example/42\n",
        html=(
            "<html><body><h1>This week in examples</h1>"
            '<p>Ten new <a href="https://newsletter.example/42">examples</a>.</p>'
            '<img src="https://newsletter.example/pixel.gif" width="1" height="1">'
            "</body></html>"
        ),
        headers=[("List-Unsubscribe", "<https://newsletter.example/unsubscribe>")],
    )
    deliver(
        "INBOX",
        timedelta(hours=3),
        sender=ALEX,
        cc=[KIM],
        subject="Invoice 2026-117 due next week",
        message_id="<invoice-117@example.com>",
        text=(
            "Dear Robin,\n\nplease find our invoice 2026-117 attached. Payment is due by "
            "the end of next week.\n\nBest regards,\nAlex\n\n-- \nAlex Sample\nExample Ltd\n"
        ),
        attachments=[("invoice-2026-117.pdf", "application/pdf", b"%PDF-1.4 synthetic\n")],
    )
    deliver(
        "Projects/Lighthouse",
        timedelta(days=40),
        flags={"\\Seen"},
        sender=KIM,
        subject="Lighthouse kick-off notes",
        message_id="<lighthouse-0@example.net>",
        text="Notes from the kick-off meeting.\n",
    )
    return fake

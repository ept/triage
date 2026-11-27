"""The report on how sent replies are laid out: what it finds, and that it reveals no mail."""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta

import pytest

from triage.gateway import GatewayClient
from triage.reply_report import KEPT_WORDS, html_skeleton, main, report, shape
from triage.store import Store
from triage.sync import SyncEngine
from triage.testing.fake_gateway import FakeGateway
from triage.testing.samples import USER, make_message

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
APPLE = ("X-Mailer", "Apple Mail (2.3826.700.81)")
# Newer versions of Mail on a Mac state their version only here.
MAC = ("MIME-Version", "1.0 (Mac OS X Mail 16.0 \\(3826.700.81\\))")

# Everything that comes from a "message" in these tests contains the word SECRET (in some
# spelling), so that one check shows that nothing of a message reaches the report.
PLAIN_REPLY = (
    "SECRET answer, with secret details.\n\n"
    "> On 3 Sept 2026, at 09:05, Secretname Secretson <secret.person@secret.example> wrote:\n"
    "> \n"
    "> A secret question?\n"
    ">> An older secret line\n"
    ">> \n"
)
HTML_REPLY = (
    '<html><head><meta http-equiv="content-type" content="text/html; charset=utf-8">'
    '<meta name="author" content="Secret Author"></head>'
    '<body style="overflow-wrap: break-word; -webkit-nbsp-mode: space; '
    'line-break: after-white-space; font-family: SecretFont; background: url(secret.png)">'
    'SECRET answer<div><br><div><blockquote type="cite"><div>On 3 Sept 2026, at 09:05, '
    "Secretname Secretson &lt;secret.person@secret.example&gt; wrote:</div>"
    '<br class="Apple-interchange-newline"><div><div class="secretclass" title="Secret title">'
    '<a href="https://secret.example/secretpath">secret link</a><img alt="secret" '
    'src="cid:secret"></div></div></blockquote></div><br></div></body></html>'
)


def fill(fake: FakeGateway, client: GatewayClient, store: Store) -> None:
    def sent(folder: str = "Sent", **kwargs: object) -> None:
        kwargs.setdefault("sender", USER)
        kwargs.setdefault("to", [("Secretname Secretson", "secret.person@secret.example")])
        kwargs.setdefault("subject", "Re: the secret subject")
        raw = make_message(date=NOW - timedelta(days=1), **kwargs)  # type: ignore[arg-type]
        fake.add_message("work", folder, raw, internal_date=NOW)

    for _ in range(3):
        sent(text=PLAIN_REPLY, in_reply_to="<secret-1@secret.example>", headers=[MAC])
    sent(
        text=PLAIN_REPLY,
        html=HTML_REPLY,
        in_reply_to="<secret-2@secret.example>",
        headers=[APPLE, ("X-Secret-Header", "secret value")],
        attachments=[("secret-file.pdf", "application/pdf", b"secret")],
    )
    # A reply whose introducing line is in a form that is not known.
    sent(
        text="Secret!\n\nAm 03.09.2026 um 09:05 schrieb Secretname <secret@secret.example>:\n> Geheim\n",
        in_reply_to="<secret-3@secret.example>",
        headers=[MAC],
    )
    # Not a reply; one written by a program that is not known (described all the same, in
    # case it is Mail after all); and one written on a phone, which is left out.
    sent(text="A new secret message.\n", headers=[MAC])
    sent(
        text=PLAIN_REPLY,
        in_reply_to="<secret-4@secret.example>",
        headers=[("User-Agent", "Secret Mail 1.0")],
    )
    sent(
        text="Secret.\n\n> On 3 Sep 2026, at 9:05 am, Secretname <s@secret.example> wrote:\n> Hi\n",
        in_reply_to="<secret-5@secret.example>",
        headers=[("X-Mailer", "iPhone Mail (23G90)")],
    )
    sent(
        folder="Drafts", text="A secret draft.\n", headers=[("X-Uniform-Type-Identifier", "secret")]
    )
    snapshot = SyncEngine(client, store, clock=lambda: NOW).sync_all()
    assert snapshot.errors == []


def test_what_the_report_finds(fake: FakeGateway, client: GatewayClient, store: Store) -> None:
    fill(fake, client, store)
    found = report(store)
    assert found.messages == 8
    assert found.replies == 6
    assert found.mailers == {
        "Mail on a Mac, version 16.0 (3826.700.81), going by Mime-Version": 5,
        "Apple Mail (2.3826.700.81)": 1,
        "another program": 1,
        "iPhone Mail (23G90)": 1,
    }
    assert found.mailer_shapes == {
        "(no such field)": 5,
        "Apple Mail (9.9999.999.99)": 1,
        "Xx Mail 9.9": 1,
        "iPhone Mail (99Xx99)": 1,
    }
    assert found.version_shapes == {
        "9.9 (Mac OS X Mail 99.9 \\(9999.999.99\\))": 5,
        "9.9": 3,
    }
    assert found.structures == {
        "text/plain": 5,
        "multipart/mixed(multipart/alternative(text/plain, text/html), application/*)": 1,
    }
    assert found.recognised == {"day first, 24-hour clock": 5}
    assert found.unrecognised == {"Am 99.99.9999 um 99:99 schrieb Xx <ADDR>:": 1}
    assert found.months == {"Sept": 5}
    assert found.padded_hours == {"09": 5}
    assert found.who == {"Xx Xx <ADDR>": 5}
    assert found.depth == {"1": 5, "0": 1}
    assert found.blank_before == {"1": 6}
    assert found.line_after["an empty quoted line, written '> '"] == 5
    assert found.line_after["quoted text"] == 1
    assert found.after_quote == {"nothing": 6}
    assert found.prefixes == {"'> '": 11, "'>> '": 5}
    assert found.empty_prefixes == {"'> '": 5, "'>> '": 5}
    assert found.draft_messages == 1
    assert "x-uniform-type-identifier" in found.draft_header_names
    assert "x-secret-header" in found.header_names, "names of header fields are shown"

    (skeleton,) = found.skeletons
    assert skeleton.split("\n") == [
        "html",
        "  head",
        '  body[style="overflow-wrap: break-word; -webkit-nbsp-mode: space; '
        'line-break: after-white-space; font-family: …; background: …"]',
        "    #text",
        "    div",
        "      br",
        "      div",
        '        blockquote[type="cite"]   <-- the quotation',
        "          div > #text",
        '          br[class="Apple-interchange-newline"]',
        "          div > div[class, title]",
        "      br   (after)",
    ]


def test_the_report_reveals_nothing_of_the_mail(
    fake: FakeGateway, client: GatewayClient, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(fake, client, store)
    assert main(["--cache-dir", str(store.cache_dir)]) == 0
    text = capsys.readouterr().out
    assert "(all but those written on an iPhone or iPad): 6" in text
    assert "On 9 Sept 9999" not in text, "known forms are counted, not shown"
    leaks = [
        line
        for line in text.splitlines()
        if "secret" in line.lower().replace("x-secret-header", "")
    ]
    assert leaks == []
    assert "geheim" not in text.lower()
    assert USER[0] not in text
    assert USER[1] not in text
    assert "2026" not in text
    assert "09:05" not in text


def test_shapes_keep_only_fixed_words() -> None:
    assert shape("On 27 Nov 2026, at 10:15 AM, Kim O'Neil <kim@example.net> wrote:") == (
        "On 99 Nov 9999, at 99:99[U+202F]AM, Xx Xx'Xx <ADDR> wrote:"
    )
    assert shape("kim@example.net schrieb am Montag:") == "ADDR schrieb am Xx:"
    assert shape("NASA said ✈ Größe 北京 ٣") == "XX xx ? Xx xx 9"
    randomness = random.Random(7)
    alphabet = "abcXYZ019 @<>.:,'\"-äÖ北 ​✈\n\t"
    allowed = KEPT_WORDS | {"Xx", "xx", "XX", "ADDR", "U"}
    for _ in range(2000):
        line = "".join(randomness.choice(alphabet) for _ in range(randomness.randrange(60)))
        result = shape(line)
        assert result.isascii()
        words = set(re.findall(r"[A-Za-z]+", re.sub(r"\[U\+[0-9A-F]{4,6}\]", " ", result)))
        assert words <= allowed, (line, result)
        assert not re.search(r"[0-8]", re.sub(r"\[U\+[0-9A-F]{4,6}\]", "", result))


def test_html_without_a_marked_quotation() -> None:
    assert html_skeleton("<p>Secret</p>") == "(no blockquote)"
    plain = html_skeleton("<body><p>Secret</p><blockquote><p>secret</p></blockquote></body>")
    assert plain.split("\n") == [
        "(no blockquote with type=cite; showing the first blockquote)",
        "body",
        "  p",
        "  blockquote   <-- the quotation",
        "    p > #text",
    ]
    # Odd names and deep or broken markup do no harm.
    odd = html_skeleton(
        '<x-Secret$ séc="1"><blockquote type=CITE onclick="secret()">' + "<div>" * 50_000
    )
    assert "secret" not in odd.lower()
    assert 'blockquote[type="CITE", onclick]' in odd

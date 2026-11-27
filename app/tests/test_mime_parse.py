"""Parsing of well-formed and of malformed messages."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from html_checks import problems

from triage.mime import (
    Address,
    Attachment,
    decode_bytes,
    html_to_text,
    parse_message,
)
from triage.testing.samples import ALEX, KIM, USER, make_message

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def message(headers: str, body: bytes | str = b"body\r\n") -> bytes:
    if isinstance(body, str):
        body = body.encode()
    return headers.strip().replace("\n", "\r\n").encode() + b"\r\n\r\n" + body


def test_simple_message() -> None:
    raw = make_message(
        sender=ALEX,
        to=[USER, KIM],
        cc=[("", "list@example.net")],
        subject="Meeting on Thursday",
        text="Hello Robin,\n\nsee you on Thursday.\n",
        date=datetime(2026, 11, 2, 9, 30, tzinfo=UTC),
        message_id="<m3@example.com>",
        in_reply_to="<m2@example.org>",
        references=["<m1@example.com>", "<m2@example.org>"],
        headers=[("Reply-To", "Alex Replies <replies@example.com>")],
    )
    parsed = parse_message(raw)
    assert parsed.subject == "Meeting on Thursday"
    assert parsed.from_ == [Address("Alex Sample", "alex@example.com")]
    assert parsed.to == [Address("Robin Tester", "robin@example.org"), Address(*KIM)]
    assert parsed.cc == [Address("", "list@example.net")]
    assert parsed.reply_to == [Address("Alex Replies", "replies@example.com")]
    assert parsed.date == datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
    assert parsed.message_id == "<m3@example.com>"
    assert parsed.in_reply_to == "<m2@example.org>"
    assert parsed.references == ["<m1@example.com>", "<m2@example.org>"]
    assert parsed.text == "Hello Robin,\n\nsee you on Thursday."
    assert parsed.html is None
    assert parsed.attachments == []
    assert parsed.problems == []
    assert str(parsed.from_[0]) == "Alex Sample <alex@example.com>"
    assert str(parsed.cc[0]) == "list@example.net"


def test_alternative_prefers_plain_text_and_keeps_html() -> None:
    parsed = parse_message(
        make_message(
            text="Plain version\n",
            html='<p>HTML <b>version</b></p><img src="https://tracker.example/p.gif">',
        )
    )
    assert parsed.text == "Plain version"
    assert parsed.html is not None
    assert "<b>version</b>" in parsed.html
    assert parsed.blocked_remote_content == 1
    assert "tracker.example" not in parsed.html
    assert problems(parsed.html) == []


def test_html_only_message_gets_derived_text() -> None:
    parsed = parse_message(
        make_message(
            text=None,
            html=(
                "<html><head><title>Ignore me</title><style>p{color:red}</style></head><body>"
                "<h1>Offer</h1><p>First&nbsp;paragraph<br>second line</p>"
                "<ul><li>one</li><li>two</li></ul>"
                "<blockquote>quoted<br>text</blockquote>"
                "<script>alert('x')</script></body></html>"
            ),
        )
    )
    assert parsed.text == (
        "Offer\n\nFirst paragraph\nsecond line\n\n- one\n- two\n\n> quoted\n> text"
    )
    assert parsed.html is not None
    assert "Ignore me" not in parsed.html
    assert "alert" not in parsed.html


def test_attachments_and_inline_images() -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "alex@example.com"
    msg["Subject"] = "With pictures"
    msg.set_content("See the picture.\n")
    msg.add_alternative('<p>See <img src="cid:pic1@example"></p>', subtype="html")
    msg.get_payload()[1].add_related(PNG, maintype="image", subtype="png", cid="<pic1@example>")
    msg.add_attachment(
        b"%PDF-1.4 synthetic", maintype="application", subtype="pdf", filename="report.pdf"
    )
    msg.add_attachment("notes\n", filename="../../etc/notes.txt")

    parsed = parse_message(msg.as_bytes())
    assert parsed.text == "See the picture."
    assert parsed.html is not None
    assert "data:image/png;base64," + base64.b64encode(PNG).decode() in parsed.html
    assert parsed.attachments == [
        Attachment(None, "image/png", len(PNG), "pic1@example", True),
        Attachment("report.pdf", "application/pdf", 18, None, False),
        Attachment("notes.txt", "text/plain", 6, None, False),
    ]


def test_attached_message_is_an_attachment_not_the_body() -> None:
    from email.message import EmailMessage

    inner = EmailMessage()
    inner["Subject"] = "Inner"
    inner.set_content("inner body\n")
    outer = EmailMessage()
    outer["Subject"] = "Outer"
    outer.set_content("outer body\n")
    outer.add_attachment(inner)
    parsed = parse_message(outer.as_bytes())
    assert parsed.subject == "Outer"
    assert parsed.text == "outer body"
    assert [a.content_type for a in parsed.attachments] == ["message/rfc822"]
    assert parsed.attachments[0].size > 0


def test_multiple_inline_text_parts_are_joined() -> None:
    raw = message(
        """
From: a@example.org
Content-Type: multipart/mixed; boundary=XX
""",
        b"--XX\r\nContent-Type: text/plain\r\n\r\nfirst part\r\n"
        b"--XX\r\nContent-Type: image/png\r\nContent-Disposition: inline\r\n\r\nxx\r\n"
        b"--XX\r\nContent-Type: text/plain\r\n\r\nsecond part\r\n--XX--\r\n",
    )
    parsed = parse_message(raw)
    assert parsed.text == "first part\n\nsecond part"
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].inline


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("=?utf-8?B?R3LDvMOfZQ==?=", "Grüße"),
        ("=?iso-8859-1?Q?Gr=FC=DFe?= aus =?utf-8?Q?K=C3=B6ln?=", "Grüße aus Köln"),
        ("=?utf-8?Q?split_?= =?utf-8?Q?words?=", "split words"),
        ("=?x-unknown-charset?Q?caf=C3=A9?=", "café"),
        ("=?utf-8?B?!!!not-base64!!!?=", None),
        ("=?utf-8?Q?unterminated", "=?utf-8?Q?unterminated"),
        ("plain\r\n folded\r\n\tsubject", "plain folded subject"),
        ("tabs\tand   spaces", "tabs and spaces"),
    ],
)
def test_subject_decoding(header: str, expected: str | None) -> None:
    parsed = parse_message(f"Subject: {header}\r\n\r\nbody".encode())
    if expected is not None:
        assert parsed.subject == expected
    assert isinstance(parsed.subject, str)


def test_raw_8bit_headers() -> None:
    utf8 = parse_message("Subject: Größe\r\nFrom: Jörg <j@example.org>\r\n\r\nx".encode())
    assert utf8.subject == "Größe"
    assert utf8.from_ == [Address("Jörg", "j@example.org")]
    latin1 = parse_message(
        "Subject: Größe\r\nFrom: Jörg <j@example.org>\r\n\r\nx".encode("latin-1")
    )
    assert latin1.subject == "Größe"
    assert latin1.from_ == [Address("Jörg", "j@example.org")]
    mixed = parse_message(b"Subject: caf\xc3\xa9 and caf\xe9\r\n\r\nx")
    assert mixed.subject == "café and café"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("alex@example.com", [("", "alex@example.com")]),
        ("<alex@example.com>", [("", "alex@example.com")]),
        ('"Sample, Alex" <alex@example.com>', [("Sample, Alex", "alex@example.com")]),
        ("=?utf-8?Q?Zo=C3=AB?= <z@example.org>", [("Zoë", "z@example.org")]),
        ("A <a@example.org>, B <b@example.org>", [("A", "a@example.org"), ("B", "b@example.org")]),
        ("Team: a@example.org, b@example.org;", [("", "a@example.org"), ("", "b@example.org")]),
        ("undisclosed-recipients:;", []),
        ("a@example.org, a@example.org", [("", "a@example.org")]),
        ("", []),
    ],
)
def test_address_parsing(header: str, expected: list[tuple[str, str]]) -> None:
    parsed = parse_message(f"To: {header}\r\n\r\nbody".encode())
    assert [(a.name, a.address) for a in parsed.to] == expected


@pytest.mark.parametrize(
    "header",
    [
        "<<<>>>",
        "@",
        '"unterminated <a@example.org>',
        "a@example.org, , ,",
        "(comment only)",
        "<a@example.org> <b@example.org> c@",
        "a" * 5000 + "@example.org",
        ", ".join(f"user{i}@example.org" for i in range(3000)),
        "\x00\x01\x02 <a@example.org>",
        "=?utf-8?B?" + "A" * 500 + "?= <a@example.org>",
    ],
)
def test_malformed_address_headers_do_not_break_parsing(header: str) -> None:
    parsed = parse_message(f"From: {header}\r\nTo: {header}\r\nSubject: s\r\n\r\nbody".encode())
    assert parsed.subject == "s"
    assert parsed.text == "body"
    assert len(parsed.to) <= 500
    for address in parsed.to:
        assert "\x00" not in address.name + address.address


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Mon, 2 Nov 2026 09:30:00 +0000", datetime(2026, 11, 2, 9, 30, tzinfo=UTC)),
        ("Mon, 2 Nov 2026 10:30:00 +0100", datetime(2026, 11, 2, 9, 30, tzinfo=UTC)),
        ("2 Nov 2026 09:30:00 -0000", datetime(2026, 11, 2, 9, 30, tzinfo=UTC)),
        ("Mon, 2 Nov 2026 09:30:00 +0000 (UTC)", datetime(2026, 11, 2, 9, 30, tzinfo=UTC)),
        ("yesterday", None),
        ("", None),
        ("Mon, 32 Nov 2026 09:30:00 +0000", None),
        ("Mon, 2 Nov 0001 00:00:00 +0000", None),
        ("Mon, 2 Nov 99999 00:00:00 +0000", None),
        ("Mon, 2 Nov 2026 09:30:00 +9999", None),
    ],
)
def test_date_parsing(header: str, expected: datetime | None) -> None:
    parsed = parse_message(f"Date: {header}\r\n\r\nbody".encode())
    if expected is None:
        # An unparseable date is no date; in no case may it be an error or a naive datetime.
        assert parsed.date is None or parsed.date.tzinfo is not None
    else:
        assert parsed.date == expected


def test_message_ids_are_normalised() -> None:
    raw = message(
        """
Message-ID:   <id1@example.org> (comment)
In-Reply-To: Your message of yesterday <id0@example.org> <other@example.org>
References: <a@example.org>
 <b@example.org>,<c@example.org> garbage <a@example.org> <unterminated
"""
    )
    parsed = parse_message(raw)
    assert parsed.message_id == "<id1@example.org>"
    assert parsed.in_reply_to == "<id0@example.org>"
    assert parsed.references == ["<a@example.org>", "<b@example.org>", "<c@example.org>"]

    without = parse_message(b"Message-ID: no-angle-brackets\r\n\r\nx")
    assert without.message_id is None


@pytest.mark.parametrize(
    ("charset", "payload", "expected"),
    [
        ("utf-8", "Größe €".encode(), "Größe €"),
        ("iso-8859-1", "Größe".encode("latin-1"), "Größe"),
        ("iso-8859-1", b"smart \x93quotes\x94 \x80", "smart “quotes” €"),
        ("us-ascii", "Größe".encode(), "Größe"),
        ("us-ascii", "Größe".encode("latin-1"), "Größe"),
        ("utf-8", "Größe".encode("latin-1"), "Größe"),
        ("windows-1251", "Привет".encode("cp1251"), "Привет"),
        ("koi8-r", "Привет".encode("koi8-r"), "Привет"),
        ("iso-2022-jp", "こんにちは".encode("iso-2022-jp"), "こんにちは"),
        ("shift_jis", "こんにちは".encode("shift_jis"), "こんにちは"),
        ("gb2312", "你好".encode("gbk"), "你好"),
        ("big5", "你好".encode("big5"), "你好"),
        ("ks_c_5601-1987", "안녕".encode("cp949"), "안녕"),
        ("utf-16", "hello".encode("utf-16"), "hello"),
        ("UTF8", "naïve".encode(), "naïve"),
        ('"utf-8"', "naïve".encode(), "naïve"),
        ("bogus-charset", "naïve".encode(), "naïve"),
        ("bogus-charset", "naïve".encode("latin-1"), "naïve"),
        ("x-unknown", "naïve".encode(), "naïve"),
        ("zlib", b"plain", "plain"),
        ("hex", b"6869", "6869"),
        ("rot13", b"plain", "plain"),
        ("unicode_escape", b"a\\u00e9b", "a\\u00e9b"),
        ("idna", b"xn--caf-dma", "xn--caf-dma"),
        ("undefined", b"plain", "plain"),
        ("", "naïve".encode(), "naïve"),
    ],
)
def test_body_charsets(charset: str, payload: bytes, expected: str) -> None:
    content_type = f"text/plain; charset={charset}" if charset else "text/plain"
    raw = (
        f"Content-Type: {content_type}\r\nContent-Transfer-Encoding: base64\r\n\r\n".encode()
        + base64.encodebytes(payload)
    )
    assert parse_message(raw).text == expected


def test_decode_bytes_without_any_hint() -> None:
    assert decode_bytes(b"") == ""
    assert decode_bytes("naïve".encode()) == "naïve"
    assert decode_bytes(b"na\xefve") == "naïve"
    assert decode_bytes(b"\x81\x8d\x8f\x90\x9d") == "�" * 5  # undefined in windows-1252


def test_transfer_encodings() -> None:
    quoted = message(
        "Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: quoted-printable",
        b"Gr=C3=BC=C3=9Fe =\r\nund mehr\r\n",
    )
    assert parse_message(quoted).text == "Grüße und mehr"
    broken_base64 = message(
        "Content-Type: text/plain\nContent-Transfer-Encoding: base64", b"aGVsbG8=!!!@@@\r\n===\r\n"
    )
    assert isinstance(parse_message(broken_base64).text, str)
    unknown = message("Content-Type: text/plain\nContent-Transfer-Encoding: x-rot26", b"hello\r\n")
    assert parse_message(unknown).text == "hello"
    eight_bit = message(
        "Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: 8bit",
        "Grüße\r\n".encode(),
    )
    assert parse_message(eight_bit).text == "Grüße"


def test_format_flowed() -> None:
    raw = message(
        "Content-Type: text/plain; charset=utf-8; format=flowed; delsp=no",
        b"This is a long paragraph that was \r\nwrapped by the mail client \r\nfor transport.\r\n"
        b"\r\n> quoted text that \r\n> was wrapped too\r\n -- stuffed\r\n-- \r\nSignature\r\n",
    )
    assert parse_message(raw).text == (
        "This is a long paragraph that was wrapped by the mail client for transport.\n\n"
        "> quoted text that was wrapped too\n-- stuffed\n-- \nSignature"
    )


def test_control_characters_and_nul_are_removed() -> None:
    parsed = parse_message(
        b"Subject: a\x00b\x07c\r\nFrom: x\x00y@example.org\r\n\r\nbody\x00 with\x1b[31m escapes\x08\r\n"
    )
    assert parsed.subject == "abc"
    assert parsed.text == "body with[31m escapes"
    assert "\x00" not in parsed.from_[0].address


def test_filenames_are_reduced_to_a_safe_last_component() -> None:
    raw = message(
        "Content-Type: multipart/mixed; boundary=XX",
        b"--XX\r\nContent-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="..\\\\..\\\\windows\\\\evil.exe"\r\n\r\nx\r\n'
        b"--XX\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename*=utf-8''%2Fetc%2Fr%C3%A9sum%C3%A9.pdf\r\n\r\nx\r\n"
        b"--XX\r\nContent-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8?B?w6kudHh0?="\r\n\r\nx\r\n'
        b'--XX\r\nContent-Type: application/octet-stream; name="a\x00b.txt"\r\n\r\nx\r\n'
        b"--XX\r\nContent-Type: weird type/not valid\r\n"
        b'Content-Disposition: attachment; filename="' + b"n" * 1000 + b'"\r\n\r\nx\r\n'
        b"--XX--\r\n",
    )
    names = [a.filename for a in parse_message(raw).attachments]
    assert names[:4] == ["evil.exe", "résumé.pdf", "é.txt", "ab.txt"]
    assert names[4] is not None
    assert len(names[4]) == 255
    types = [a.content_type for a in parse_message(raw).attachments]
    assert types[4] in ("application/octet-stream", "text/plain")


def test_html_to_text_details() -> None:
    assert html_to_text("a<br>b<div>c</div><div>d</div>e") == "a\nb\nc\nd\ne"
    assert html_to_text("a<p>b</p>c<br><br>d<br>e") == "a\n\nb\n\nc\n\nd\ne"
    assert html_to_text("<p>one</p><p></p><p></p><p>two</p>") == "one\n\ntwo"
    assert html_to_text("<pre>  keep\n\n    spacing</pre>") == "  keep\n\n    spacing"
    assert (
        html_to_text("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>") == "a b\nc"
    )
    assert html_to_text('<img alt="Chart of sales"><img src="x">') == "[Chart of sales]"
    assert html_to_text("&lt;not a tag&gt; &amp; &euro; &#x263A;") == "<not a tag> & € ☺"
    assert html_to_text("<blockquote>a<blockquote>b</blockquote>c</blockquote>d") == (
        "> a\n>\n>> b\n>\n> c\n\nd"
    )
    assert html_to_text('<a href="https://ok.example/">link text</a>') == "link text"
    assert html_to_text("<style>x{}</style><script>y()</script>visible") == "visible"
    assert html_to_text("") == ""
    assert html_to_text("<" * 10_000) is not None


def test_empty_and_degenerate_inputs() -> None:
    for raw in (
        b"",
        b"\r\n",
        b"\n\n\n",
        b"no headers at all",
        b"\x00" * 100,
        b":\r\n\r\n",
        b"\xff\xfe",
    ):
        parsed = parse_message(raw)
        assert isinstance(parsed.text, str)
        assert parsed.attachments == []


def test_deeply_nested_multipart_is_bounded() -> None:
    depth = 600
    parts = []
    for level in range(depth):
        parts.append(f"Content-Type: multipart/mixed; boundary=b{level}\r\n\r\n--b{level}\r\n")
    parts.append("Content-Type: text/plain\r\n\r\ndeep text\r\n")
    for level in reversed(range(depth)):
        parts.append(f"--b{level}--\r\n")
    parsed = parse_message("".join(parts).encode())
    assert isinstance(parsed.text, str)
    assert parsed.problems  # it was noticed that the structure is absurd


def test_very_many_parts_are_bounded() -> None:
    body = b"".join(
        b"--XX\r\nContent-Type: application/octet-stream\r\n\r\nx\r\n" for _ in range(3000)
    )
    raw = message("Content-Type: multipart/mixed; boundary=XX", body + b"--XX--\r\n")
    parsed = parse_message(raw)
    assert len(parsed.attachments) <= 1000
    assert parsed.problems


def test_huge_body_is_truncated() -> None:
    raw = message("Content-Type: text/plain", b"word " * 1_000_000)
    assert len(parse_message(raw).text) <= 2_000_000
    html = message("Content-Type: text/html", b"<p>word</p>" * 400_000)
    parsed = parse_message(html)
    assert parsed.html is not None
    assert len(parsed.html) <= 1_100_000
    assert parsed.problems == ["HTML body too large or too deeply nested; truncated"]
    assert len(parsed.text) <= 2_000_000


def test_multipart_without_boundary_or_parts() -> None:
    parsed = parse_message(message("Content-Type: multipart/mixed", b"just text really\r\n"))
    assert parsed.text == "just text really"
    parsed = parse_message(
        message("Content-Type: multipart/alternative; boundary=XX", b"--XX--\r\n")
    )
    assert parsed.text == ""


def test_too_many_boundaries_are_not_parsed() -> None:
    body = b"--XX\r\nContent-Type: text/plain\r\n\r\npart\r\n" * 10_001
    raw = message("Subject: Many parts\nContent-Type: multipart/mixed; boundary=XX", body)
    parsed = parse_message(raw)
    assert parsed.subject == "Many parts"
    assert parsed.problems == ["too many MIME boundaries; the body was not parsed"]
    assert parsed.attachments == []
    assert parsed.text.startswith("--XX\nContent-Type: text/plain")


def test_pathological_inputs_are_handled_quickly() -> None:
    """Inputs built to make parsers slow. Each takes minutes if the guards are removed."""
    import time

    cases = {
        "garbage address header": b"To: " + b"<" * 16_000 + b"\r\n\r\nx",
        "many encoded words": b"Subject: " + b" ".join([b"=?utf-8?B?w6k=?="] * 5000) + b"\r\n\r\nx",
        "megabyte header": b"X-Junk: " + b"a" * 1_000_000 + b"\r\nSubject: s\r\n\r\nx",
        "deeply nested html": b"Content-Type: text/html\r\n\r\n" + b"<div><blockquote>" * 110_000,
        "nested with stray end tags": b"Content-Type: text/html\r\n\r\n"
        + b"<div></x><q></td>" * 100_000,
        "nested behind special elements": b"Content-Type: text/html\r\n\r\n"
        + b"<span><div></span>" * 100_000,
        "nested lists": b"Content-Type: text/html\r\n\r\n" + b"<ul><li>" * 200_000,
        "nested tables": b"Content-Type: text/html\r\n\r\n" + b"<table><tr><td>" * 120_000,
        "unclosed brackets": b"Content-Type: text/html\r\n\r\n" + b"<" * 2_000_000,
        "unclosed comment": b"Content-Type: text/html\r\n\r\n<!--" + b"x-" * 1_000_000,
        "many parts": b"Content-Type: multipart/mixed; boundary=X\r\n\r\n"
        + b"--X\r\n\r\n" * 200_000,
        "many quoted lines": b"\r\n" + b"> quoted\r\n" * 300_000,
    }
    for label, raw in cases.items():
        started = time.perf_counter()
        parsed = parse_message(raw)
        elapsed = time.perf_counter() - started
        assert elapsed < 10, f"{label} took {elapsed:.1f}s"
        assert isinstance(parsed.text, str)
        if parsed.html is not None:
            assert problems(parsed.html) == []


def test_text_attachment_is_not_the_body() -> None:
    raw = message(
        "Content-Type: multipart/mixed; boundary=XX",
        b"--XX\r\nContent-Type: text/plain\r\n\r\nthe body\r\n"
        b"--XX\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename=log.txt\r\n\r\n"
        b"attached log\r\n"
        b"--XX\r\nContent-Type: text/plain; name=inline.txt\r\nContent-Disposition: inline\r\n\r\n"
        b"a file shown inline\r\n"
        b"--XX\r\nContent-Type: text/html\r\nContent-Disposition: attachment\r\n\r\n"
        b"<p>saved page</p>\r\n--XX--\r\n",
    )
    parsed = parse_message(raw)
    assert parsed.text == "the body"
    assert parsed.html is None
    assert [(a.filename, a.content_type, a.inline) for a in parsed.attachments] == [
        ("log.txt", "text/plain", False),
        ("inline.txt", "text/plain", True),
        (None, "text/html", False),
    ]


def test_parser_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    import triage.mime as mime

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(mime, "_collect", explode)
    parsed = parse_message(make_message(subject="Still here"))
    assert parsed.subject == "Still here"
    assert parsed.problems == ["could not read message body (RuntimeError)"]

    monkeypatch.setattr(mime, "_parse_structure", explode)
    parsed = parse_message(make_message())
    assert parsed.problems == ["parser failed (RuntimeError)"]

    monkeypatch.undo()
    monkeypatch.setattr(mime.email, "message_from_bytes", explode)
    parsed = parse_message(make_message(subject="Headers survive", text="and the text\n"))
    assert parsed.subject == "Headers survive"
    assert "could not parse message structure (RuntimeError)" in parsed.problems

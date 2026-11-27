"""parse_message must never raise, and its output must always be storable and safe."""

from __future__ import annotations

from datetime import datetime

from html_checks import problems
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from triage.mime import ParsedMessage, parse_message

_HEADER_NAMES = st.sampled_from(
    [
        "From", "To", "Cc", "Reply-To", "Sender", "Subject", "Date", "Message-ID", "In-Reply-To",
        "References", "Content-Type", "Content-Transfer-Encoding", "Content-Disposition",
        "Content-ID", "MIME-Version", "X-Other",
    ]
)  # fmt: skip

_HEADER_VALUES = st.one_of(
    st.sampled_from(
        [
            "text/plain", "text/html; charset=utf-8", "text/plain; charset=bogus",
            'multipart/mixed; boundary="XX"', "multipart/alternative; boundary=XX",
            "multipart/related; boundary=XX; start=<a>", "message/rfc822", "multipart/mixed",
            "image/png; name*0=a; name*1=b", "application/octet-stream; name==?utf-8?B?w6k=?=",
            "text/plain; format=flowed; delsp=yes", "text/plain; charset*=utf-8''%zz",
            "base64", "quoted-printable", "8bit", "7bit", "binary", "x-uuencode", "nonsense",
            "attachment; filename=x.txt", "inline", "attachment; filename*=utf-8''%E9%",
            "attachment; filename=\"a\\\"b\"", "<id@example.org>", "<a@b> <c@d>", "<>", "<",
            "Mon, 2 Nov 2026 09:30:00 +0000", "32 Foo 99999 99:99:99 +9999", "1 Jan 1 0:0:0",
            "Alex Sample <alex@example.com>", '"unterminated <a@example.org>', "a@b, c@d;",
            "group: a@example.org, b@example.org;", "=?utf-8?B?w6k=?= <a@example.org>",
            "=?bogus?Q?x?=", "=?utf-8?B?!!!?=", "=?utf-8?Q?=?=", "=?utf-8?X?abc?=", "(comment",
            "\\", '"', ",,,,", "@@@", "1.0",
        ]
    ),
    st.text(max_size=40),
    st.binary(max_size=40).map(lambda b: b.decode("latin-1")),
)  # fmt: skip

_BODY_PIECES = st.one_of(
    st.sampled_from(
        [
            b"--XX\r\n", b"--XX--\r\n", b"--YY\r\n", b"\r\n", b"\n", b"\r",
            b"Content-Type: text/plain\r\n\r\n", b"Content-Type: text/html\r\n\r\n",
            b"Content-Type: multipart/mixed; boundary=YY\r\n\r\n",
            b"Content-Type: message/rfc822\r\n\r\n", b"Content-Type: image/png\r\n",
            b"Content-ID: <logo>\r\n", b"Content-Transfer-Encoding: base64\r\n",
            b"Content-Transfer-Encoding: quoted-printable\r\n",
            b"Content-Disposition: attachment; filename=a.txt\r\n",
            b"Subject: inner\r\n", b"aGVsbG8gd29ybGQ=\r\n", b"=C3=A9=\r\n", b"=ZZ", b"====",
            b"hello world\r\n", b"<p>hi <img src=cid:logo><img src=http://evil.example/x></p>",
            b"<script>alert(1)</script>", b"<a href=javascript:alert(1)>x</a>",
            b"> quoted\r\n", b"-- \r\n", b"On Monday, Kim wrote:\r\n", b"\x00", b"\xff\xfe",
            b"\xc3\xa9", b"\xe9", b"\x1b$B", b"begin 644 file\r\n",
        ]
    ),
    st.binary(max_size=30),
)  # fmt: skip


_CONTENT_TYPES = st.sampled_from(
    [
        "text/plain", "text/plain; charset=utf-8", "text/plain; charset=bogus",
        "text/plain; charset=iso-2022-jp", "text/plain; format=flowed; delsp=yes",
        "text/html", "text/html; charset=utf-8", "text/html; charset=utf-16",
        "TEXT/HTML; CHARSET=\"us-ascii\"", "text/plain; charset*=utf-8''%zz",
        "image/png", "image/png; name=logo.png", "image/svg+xml", "application/pdf",
        "application/octet-stream; name==?utf-8?B?w6k=?=", "image/png; name*0=a; name*1=b",
        "message/rfc822", "multipart/digest; boundary=YY", "nonsense", "text/", "/", "",
    ]
)  # fmt: skip
_ENCODINGS = st.sampled_from(
    ["7bit", "8bit", "binary", "base64", "BASE64", "quoted-printable", "x-uuencode", "nonsense"]
)
_DISPOSITIONS = st.sampled_from(
    [
        "inline", "attachment", "attachment; filename=x.txt", "attachment; filename=\"a/b\\c\"",
        "attachment; filename*=utf-8''%E9%", "inline; filename=logo.png", "form-data", ";;;",
    ]
)  # fmt: skip
_PART_HEADERS = st.one_of(
    st.tuples(st.just("Content-Type"), _CONTENT_TYPES),
    st.tuples(st.just("Content-Transfer-Encoding"), _ENCODINGS),
    st.tuples(st.just("Content-Disposition"), _DISPOSITIONS),
    st.tuples(st.just("Content-ID"), st.sampled_from(["<logo>", "logo", "<>", "<a b>"])),
    st.tuples(_HEADER_NAMES, _HEADER_VALUES),
)


def _render_headers(headers: list[tuple[str, str]]) -> bytes:
    return b"".join(
        name.encode() + b": " + value.encode("latin-1", "replace") + b"\r\n"
        for name, value in headers
    )


@st.composite
def _parts(draw: st.DrawFn, depth: int = 0) -> bytes:
    """A MIME entity: headers, a blank line, and a body that may itself be multipart."""
    headers = draw(st.lists(_PART_HEADERS, max_size=5))
    kind = draw(st.sampled_from(["leaf", "leaf", "multi", "message"])) if depth < 3 else "leaf"
    if kind == "multi":
        boundary = f"B{depth}"
        subtype = draw(st.sampled_from(["mixed", "alternative", "related", "signed", "report"]))
        headers.append(("Content-Type", f"multipart/{subtype}; boundary={boundary}"))
        children = draw(st.lists(_parts(depth + 1), max_size=4))
        closing = draw(st.sampled_from([f"--{boundary}--\r\n", "", f"--{boundary}\r\n"]))
        body = b"".join(f"--{boundary}\r\n".encode() + child + b"\r\n" for child in children)
        body += closing.encode()
    elif kind == "message":
        headers.append(("Content-Type", "message/rfc822"))
        body = draw(_parts(depth + 1))
    else:
        body = b"".join(draw(st.lists(_BODY_PIECES, max_size=8)))
    return _render_headers(headers) + b"\r\n" + body


@st.composite
def _chaos(draw: st.DrawFn) -> bytes:
    """Headers and body pieces thrown together without regard for structure."""
    headers = draw(st.lists(st.tuples(_HEADER_NAMES, _HEADER_VALUES), max_size=10))
    separator = draw(st.sampled_from([b"\r\n", b"\n", b"\r\n\t", b"\r\n "]))
    head = b"".join(
        name.encode() + b": " + value.encode("latin-1", "replace") + separator.rstrip(b"\t ")
        for name, value in headers
    )
    body = b"".join(draw(st.lists(_BODY_PIECES, max_size=25)))
    return head + b"\r\n" + body


def messages() -> st.SearchStrategy[bytes]:
    return st.one_of(_parts(), _chaos())


def check(parsed: ParsedMessage) -> None:
    """The invariants that the rest of the app relies on."""
    strings = [parsed.subject, parsed.text, parsed.html or ""]
    strings += [parsed.message_id or "", parsed.in_reply_to or "", *parsed.references]
    for address in (*parsed.from_, *parsed.sender, *parsed.to, *parsed.cc, *parsed.reply_to):
        strings += [address.name, address.address]
    for attachment in parsed.attachments:
        strings += [attachment.filename or "", attachment.content_type, attachment.content_id or ""]
        assert attachment.size >= 0
        assert "/" not in (attachment.filename or "")
        assert "\\" not in (attachment.filename or "")
    for value in strings:
        assert isinstance(value, str)
        value.encode("utf-8")  # no lone surrogates: must be storable
        assert "\x00" not in value
    assert parsed.date is None or (
        isinstance(parsed.date, datetime) and parsed.date.tzinfo is not None
    )
    if parsed.html is not None:
        assert problems(parsed.html) == []
    for reference in (parsed.message_id, parsed.in_reply_to, *parsed.references):
        assert reference is None or (reference.startswith("<") and reference.endswith(">"))


@settings(max_examples=1500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(messages())
def test_structured_garbage(raw: bytes) -> None:
    check(parse_message(raw))


@settings(max_examples=1000, deadline=None)
@given(st.binary(max_size=400))
def test_random_bytes(raw: bytes) -> None:
    check(parse_message(raw))


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=200), st.text(max_size=200), st.text(max_size=400))
def test_unicode_everywhere(subject: str, sender: str, body: str) -> None:
    raw = f"Subject: {subject}\r\nFrom: {sender}\r\n\r\n{body}".encode("utf-8", "surrogatepass")
    check(parse_message(raw))

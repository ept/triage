"""Parsing of raw email messages into a form that is safe to store, search, and display.

Everything in an email is attacker-controlled, so the two rules of this module are:

* :func:`parse_message` never raises, whatever bytes it is given. Malformed structure, unknown
  or wrongly declared character sets, and absurd nesting all degrade to a best-effort result.
* The HTML returned by :func:`sanitize_html` cannot run scripts and cannot cause anything to be
  fetched from the network when it is rendered. This is one layer of several: the user interface
  additionally renders email in an isolated frame, and the wrapper app blocks remote loads.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import email
import email.errors
import email.header
import email.parser
import email.policy
import email.utils
import html as html_module
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from html.parser import HTMLParser
from typing import Any

import nh3

# Limits that keep hostile input from consuming unbounded time and memory.
MAX_TEXT_CHARS = 2_000_000
MAX_HTML_CHARS = 1_000_000
MAX_HTML_DEPTH = 500
MAX_BOUNDARY_LINES = 10_000
MAX_HEADER_CHARS = 16_384
MAX_SUBJECT_CHARS = 2_000
MAX_ADDRESSES = 500
MAX_REFERENCES = 200
MAX_PARTS = 1_000
MAX_DEPTH = 40
MAX_ATTACHMENTS = 500
MAX_INLINE_IMAGE_BYTES = 1_000_000
MAX_INLINE_IMAGES_TOTAL_BYTES = 8_000_000
MAX_URL_CHARS = 8_192

INLINE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

BLOCKED_SRC_ATTRIBUTE = "data-triage-blocked-src"
"""Marks an ``img`` whose remote source was removed, so that the UI can say so."""


# --------------------------------------------------------------------------------------------
# Result types


@dataclass(frozen=True, slots=True)
class Address:
    name: str
    address: str

    def __str__(self) -> str:
        if self.name and self.address:
            return f"{self.name} <{self.address}>"
        return self.address or self.name


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str | None
    content_type: str
    size: int
    content_id: str | None
    inline: bool


@dataclass(slots=True)
class ParsedMessage:
    subject: str = ""
    from_: list[Address] = field(default_factory=list)
    sender: list[Address] = field(default_factory=list)
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    reply_to: list[Address] = field(default_factory=list)
    date: datetime | None = None
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    text: str = ""
    """The best plain-text rendering of the body."""
    html: str | None = None
    """Sanitised HTML body, if the message has one. Safe to render (see module docstring)."""
    attachments: list[Attachment] = field(default_factory=list)
    blocked_remote_content: int = 0
    """Number of remote resources (e.g. images) that were removed from the HTML."""
    problems: list[str] = field(default_factory=list)
    """Notes about anything that had to be worked around while parsing."""


@dataclass(frozen=True, slots=True)
class SanitizedHtml:
    html: str
    blocked_remote_content: int
    truncated: bool = False
    """Whether the input was cut short because it was too long or nested too deeply."""


@dataclass(frozen=True, slots=True)
class SplitText:
    """A plain-text body divided into what the author wrote and what they merely carried along."""

    body: str
    signature: str
    quoted: str


# --------------------------------------------------------------------------------------------
# Text helpers

_SURROGATES = re.compile(r"[\ud800-\udfff]")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_CHARSET_ALIASES = {
    # Per the WHATWG encoding standard, these labels mean windows-1252 in practice.
    "iso-8859-1": "cp1252",
    "iso8859-1": "cp1252",
    "latin1": "cp1252",
    "latin-1": "cp1252",
    "us-ascii": "cp1252",
    "ascii": "cp1252",
    "ansi_x3.4-1968": "cp1252",
    "utf8": "utf-8",
    "unicode-1-1-utf-8": "utf-8",
    "unicode-1-1-utf-7": "utf-7",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "x-gbk": "gb18030",
    "ks_c_5601-1987": "cp949",
    "iso-8859-8-i": "iso-8859-8",
    "iso-8859-8-e": "iso-8859-8",
    "windows-874": "cp874",
    "x-sjis": "shift_jis",
    "x-user-defined": "cp1252",
    "x-unknown": "utf-8",
    "unknown-8bit": "utf-8",
    "default": "utf-8",
}


_REFUSED_CODECS = frozenset(
    {"unicode_escape", "raw_unicode_escape", "idna", "punycode", "undefined", "mbcs", "oem"}
)


def clean_text(text: str) -> str:
    """Make a string safe to store: no lone surrogates, no NULs or other control characters."""
    if _SURROGATES.search(text):
        text = _SURROGATES.sub("�", text)
    return _CONTROL_CHARS.sub("", text)


_CP1252_FALLBACK = "triage-cp1252-fallback"


def _decode_invalid_as_cp1252(error: UnicodeError) -> tuple[str, int]:
    if not isinstance(error, UnicodeDecodeError):
        raise error
    invalid = error.object[error.start : error.end]
    return invalid.decode("cp1252", "replace"), error.end


codecs.register_error(_CP1252_FALLBACK, _decode_invalid_as_cp1252)


def _lookup_codec(charset: str | None) -> str | None:
    if not charset:
        return None
    name = charset.strip().strip("\"'").lower()
    name = _CHARSET_ALIASES.get(name, name)
    try:
        info = codecs.lookup(name)
    except (LookupError, ValueError, TypeError):
        return None
    # Python also registers codecs that are not character sets (zlib, hex, rot13, ...) and
    # some that are but have no business being used for email (escapes, punycode).
    if not getattr(info, "_is_text_encoding", True):
        return None
    if info.name.replace("-", "_") in _REFUSED_CODECS:
        return None
    return info.name


def decode_bytes(data: bytes, charset: str | None = None) -> str:
    """Decode text of doubtful provenance.

    The declared character set is used if it is known and fits. If it is unknown, missing, or
    wrong, UTF-8 is tried, and the last resort is windows-1252 with replacement characters.
    """
    codec = _lookup_codec(charset)
    if codec is not None and codec not in ("cp1252",):
        try:
            return data.decode(codec)
        except Exception:  # noqa: S110 - any failure means "try the next guess"
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if codec is not None and codec not in ("cp1252", "utf-8"):
        try:
            return data.decode(codec, "replace")
        except Exception:  # noqa: S110
            pass
    # Mostly windows-1252, but text that is UTF-8 apart from a few stray bytes stays readable.
    return data.decode("utf-8", _CP1252_FALLBACK)


def _fix_surrogates(value: str) -> str:
    """Recover text from a header that contained raw 8-bit bytes.

    The email parser decodes headers as ASCII with ``surrogateescape``, so undeclared 8-bit
    bytes arrive as lone surrogates. Turn them back into bytes and decode those properly.
    """
    if not _SURROGATES.search(value):
        return value
    try:
        raw = value.encode("utf-8", "surrogateescape")
    except UnicodeError:
        return _SURROGATES.sub("�", value)
    return decode_bytes(raw)


# --------------------------------------------------------------------------------------------
# Headers

_MESSAGE_ID = re.compile(r"<[^<>\s]{1,995}>")
_ENCODED_WORD = re.compile(r"=\?[^?\s]+\?[bBqQ]\?[^?\s]*\?=")
_header_factory = email.policy.default.header_factory


def _raw_header(message: Message, name: str) -> str | None:
    """The first header with this name, unfolded, with 8-bit bytes repaired."""
    wanted = name.lower()
    try:
        items = message.raw_items()
    except Exception:
        return None
    for key, value in items:
        if isinstance(key, str) and key.lower() == wanted:
            if not isinstance(value, str):
                value = str(value)
            value = _fix_surrogates(value[: MAX_HEADER_CHARS * 2])
            value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "")
            return value[:MAX_HEADER_CHARS]
    return None


def _decode_encoded_words(value: str) -> str:
    """Fallback decoding of RFC 2047 encoded words for when the structured parser gives up."""

    def replace(match: re.Match[str]) -> str:
        try:
            parts = email.header.decode_header(match.group(0))
        except Exception:
            return match.group(0)
        out = []
        for data, charset in parts:
            out.append(decode_bytes(data, charset) if isinstance(data, bytes) else data)
        return "".join(out)

    # Whitespace between adjacent encoded words is not part of the text.
    value = re.sub(r"(\?=)\s+(=\?)", r"\1\2", value)
    return _ENCODED_WORD.sub(replace, value)


def _text_header(message: Message, name: str, limit: int = MAX_HEADER_CHARS) -> str:
    raw = _raw_header(message, name)
    if raw is None:
        return ""
    try:
        text = str(_header_factory(name, raw))
    except Exception:
        text = _decode_encoded_words(raw)
    if "=?" in text:
        text = _decode_encoded_words(text)
    return clean_text(" ".join(text.split()))[:limit]


def _address_header(message: Message, name: str) -> list[Address]:
    raw = _raw_header(message, name)
    if not raw:
        return []
    pairs: list[tuple[str, str]] = []
    try:
        header: Any = _header_factory(name, raw)
        for parsed in header.addresses[: MAX_ADDRESSES * 2]:
            pairs.append((str(parsed.display_name or ""), str(parsed.addr_spec or "")))
    except Exception:
        pairs = []
        try:
            for display, address in email.utils.getaddresses([raw])[: MAX_ADDRESSES * 2]:
                pairs.append((_decode_encoded_words(display), address))
        except Exception:
            pairs = []
    addresses = []
    seen = set()
    for display, address in pairs:
        display = clean_text(" ".join(display.split()))[:500]
        address = clean_text("".join(address.split()))[:320]
        if address in ("<>", "@"):
            address = ""
        if not display and not address:
            continue
        key = (display, address.lower())
        if key in seen:
            continue
        seen.add(key)
        addresses.append(Address(display, address))
        if len(addresses) >= MAX_ADDRESSES:
            break
    return addresses


def _message_ids(message: Message, name: str, limit: int) -> list[str]:
    raw = _raw_header(message, name)
    if not raw:
        return []
    ids = []
    seen = set()
    for match in _MESSAGE_ID.finditer(raw):
        value = clean_text(match.group(0))
        if value not in seen:
            seen.add(value)
            ids.append(value)
            if len(ids) >= limit:
                break
    return ids


def _date_header(message: Message) -> datetime | None:
    raw = _raw_header(message, "date")
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
        # Reject dates that cannot be converted to a timestamp or are obviously nonsense.
        if not 1970 <= parsed.year <= 9000:
            return None
        return parsed
    except Exception:
        return None


# --------------------------------------------------------------------------------------------
# Body structure


@dataclass(slots=True)
class _Bodies:
    plain: list[str] = field(default_factory=list)
    html: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Walk:
    parts_seen: int = 0
    attachments: list[Attachment] = field(default_factory=list)
    inline_images: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    inline_bytes: int = 0
    problems: list[str] = field(default_factory=list)


def _safe[T](function: Callable[[], T], default: T) -> T:
    try:
        return function()
    except Exception:
        return default


def _content_type(part: Message) -> str:
    value = _safe(part.get_content_type, "application/octet-stream")
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", value
    ):
        return "application/octet-stream"
    return value[:200]


def _payload_bytes(part: Message) -> bytes:
    payload = _safe(lambda: part.get_payload(decode=True), None)
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8", "surrogateescape")
    return b""


def _unflow(text: str, delsp: bool) -> str:
    """Undo ``format=flowed`` line wrapping (RFC 3676)."""
    out: list[str] = []
    pending: str | None = None
    pending_depth = 0
    for line in text.split("\n"):
        depth = len(line) - len(line.lstrip(">"))
        content = line[depth:]
        if content.startswith(" "):
            content = content[1:]  # space-stuffing
        flowed = content.endswith(" ") and content != "-- "
        if pending is not None and depth == pending_depth:
            pending += content
        else:
            if pending is not None:
                out.append(">" * pending_depth + (" " if pending_depth else "") + pending)
            pending, pending_depth = content, depth
        if flowed:
            if delsp:
                pending = pending[:-1]
        else:
            out.append(">" * pending_depth + (" " if pending_depth else "") + pending)
            pending = None
    if pending is not None:
        out.append(">" * pending_depth + (" " if pending_depth else "") + pending)
    return "\n".join(out)


def _text_of(part: Message) -> str:
    data = _payload_bytes(part)[: MAX_TEXT_CHARS * 4]
    charset = _safe(part.get_content_charset, None)
    text = decode_bytes(data, charset if isinstance(charset, str) else None)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if _safe(lambda: str(part.get_param("format", "")).lower(), "") == "flowed":
        delsp = _safe(lambda: str(part.get_param("delsp", "")).lower(), "") == "yes"
        text = _safe(lambda: _unflow(text, delsp), text)
    return clean_text(text)[:MAX_TEXT_CHARS]


def _filename(part: Message) -> str | None:
    name = _safe(part.get_filename, None)
    if not isinstance(name, str) or not name:
        return None
    name = clean_text(_decode_encoded_words(_fix_surrogates(name)))
    # Only the last path component, so that a filename can never be mistaken for a path.
    name = re.split(r"[\\/]", name)[-1].strip()
    return name[:255] or None


def _content_id(part: Message) -> str | None:
    raw = _raw_header(part, "content-id")
    if not raw:
        return None
    value = clean_text(raw.strip().strip("<>").strip())
    return value[:500] or None


def _children(part: Message) -> list[Message]:
    payload = _safe(part.get_payload, None)
    if isinstance(payload, list):
        return [child for child in payload if isinstance(child, Message)]
    return []


def _attached_message_size(message: Message) -> int:
    """The size of an attached message, which has to be serialised again to be measured."""
    for policy in (None, email.policy.compat32):
        try:
            return len(message.as_bytes(policy=policy))
        except Exception:  # noqa: S112 - a malformed message may not survive the round trip
            continue
    return 0


def _collect(part: Message, depth: int, walk: _Walk) -> _Bodies:
    """Find the body text and the attachments below ``part``."""
    bodies = _Bodies()
    walk.parts_seen += 1
    if walk.parts_seen > MAX_PARTS or depth > MAX_DEPTH:
        if "message structure too large; some parts were ignored" not in walk.problems:
            walk.problems.append("message structure too large; some parts were ignored")
        return bodies

    content_type = _content_type(part)
    maintype = content_type.split("/", 1)[0]
    disposition = _safe(part.get_content_disposition, None)
    children = _children(part)

    if maintype == "multipart" and children:
        collected = [_collect(child, depth + 1, walk) for child in children]
        if content_type == "multipart/alternative":
            # The alternatives say the same thing in different formats: take one of each.
            for candidate in collected:
                if not bodies.plain and any(t.strip() for t in candidate.plain):
                    bodies.plain = candidate.plain
                if not bodies.html and any(t.strip() for t in candidate.html):
                    bodies.html = candidate.html
        else:
            for candidate in collected:
                bodies.plain.extend(candidate.plain)
                bodies.html.extend(candidate.html)
        return bodies

    if maintype == "message" and children:
        # An attached message. It is listed as an attachment and not descended into.
        size = _attached_message_size(children[0])
        _add_attachment(walk, Attachment(_filename(part), content_type, size, None, False))
        return bodies

    filename = _filename(part)
    # A part with a filename is a file, even if it is text and marked for inline display.
    is_attachment = disposition == "attachment" or filename is not None
    if content_type in ("text/plain", "text/html") and not is_attachment:
        text = _text_of(part)
        (bodies.plain if content_type == "text/plain" else bodies.html).append(text)
        return bodies

    if maintype == "multipart":
        # Declared as multipart but without parts (no boundary, or the structure was not
        # parsed): what there is can at least be read as text.
        text = _text_of(part)
        if text.strip():
            bodies.plain.append(text)
        return bodies

    data = _payload_bytes(part)
    content_id = _content_id(part)
    inline = disposition == "inline" or (content_id is not None and disposition != "attachment")
    _add_attachment(walk, Attachment(filename, content_type, len(data), content_id, inline))
    if (
        content_id is not None
        and content_type in INLINE_IMAGE_TYPES
        and len(data) <= MAX_INLINE_IMAGE_BYTES
        and walk.inline_bytes + len(data) <= MAX_INLINE_IMAGES_TOTAL_BYTES
        and content_id not in walk.inline_images
    ):
        walk.inline_images[content_id] = (content_type, data)
        walk.inline_bytes += len(data)
    return bodies


def _add_attachment(walk: _Walk, attachment: Attachment) -> None:
    if len(walk.attachments) < MAX_ATTACHMENTS:
        walk.attachments.append(attachment)


# --------------------------------------------------------------------------------------------
# HTML to text

_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "center", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tr", "ul",
    }
)  # fmt: skip
# Block elements that are set apart from their surroundings by a blank line.
_PARAGRAPH_TAGS = frozenset(
    {"blockquote", "dl", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "ol", "p", "pre", "table", "ul"}
)
_SKIPPED_TAGS = frozenset(
    {"script", "style", "head", "title", "noscript", "template", "svg", "math", "object",
     "select", "textarea"}
)  # fmt: skip
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "wbr", "col", "area", "base"})


class _TextExtractor(HTMLParser):
    """Renders HTML as plain text, with quoted material marked by ``>`` as in plain-text mail."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._current: list[str] = []
        self._skip_depth = 0
        self._quote_depth = 0
        self._pre_depth = 0
        self._size = 0
        # Quote depth of a blank line that is due before the next line of text, if any.
        self._want_blank: int | None = None

    def _prefix(self) -> str:
        return ">" * self._quote_depth + (" " if self._quote_depth else "")

    def _blank(self) -> None:
        depth = self._quote_depth if self._want_blank is None else self._want_blank
        if self.lines and self.lines[-1].strip(" >"):
            self.lines.append(">" * depth)
        self._want_blank = None

    def _flush(self) -> bool:
        """End the current line. Returns whether there was any text on it."""
        text = "".join(self._current)
        if not self._pre_depth:
            text = " ".join(text.split())
        self._current = []
        if not text.strip():
            return False
        if self._want_blank is not None:
            self._blank()
        self.lines.append(self._prefix() + text)
        return True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            if not self._flush() and self._want_blank is None:
                self._blank()
        elif tag in _BLOCK_TAGS:
            self._flush()
            if tag in _PARAGRAPH_TAGS:
                self._want_blank = self._quote_depth
        if tag == "blockquote":
            self._quote_depth = min(self._quote_depth + 1, 20)
        elif tag == "pre":
            self._pre_depth += 1
        elif tag == "li":
            self._current.append("- ")
        elif tag in ("td", "th"):
            self._current.append(" ")
        elif tag == "img":
            alt = next((value for name, value in attrs if name == "alt" and value), None)
            if alt:
                self._current.append(f"[{alt}]")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "blockquote":
            self._flush()
            self._quote_depth = max(0, self._quote_depth - 1)
            self._want_blank = self._quote_depth
        elif tag in _BLOCK_TAGS:
            self._flush()
            if tag in _PARAGRAPH_TAGS:
                self._want_blank = self._quote_depth
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._size > MAX_TEXT_CHARS:
            return
        self._size += len(data)
        if self._pre_depth and "\n" in data:
            first, *rest = data.split("\n")
            self._current.append(first)
            for line in rest:
                if not self._flush():
                    self._blank()
                self._current.append(line)
        else:
            self._current.append(data)

    def result(self) -> str:
        self._flush()
        while self.lines and not self.lines[-1].strip(" >"):
            self.lines.pop()
        return "\n".join(self.lines)


def html_to_text(html: str) -> str:
    """A plain-text rendering of HTML, for search, for the LLM, and as a display fallback."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html[:MAX_HTML_CHARS])
        extractor.close()
        text = extractor.result()
    except Exception:
        text = html_module.unescape(re.sub(r"<[^>]{0,2000}>", " ", html[:MAX_HTML_CHARS]))
        text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    return clean_text(text)[:MAX_TEXT_CHARS]


# --------------------------------------------------------------------------------------------
# HTML sanitising

_ALLOWED_TAGS = frozenset(
    {
        "a", "abbr", "acronym", "address", "b", "bdi", "bdo", "big", "blockquote", "br",
        "caption", "center", "cite", "code", "col", "colgroup", "dd", "del", "dfn", "div", "dl",
        "dt", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd",
        "li", "mark", "ol", "p", "pre", "q", "s", "samp", "small", "span", "strike", "strong",
        "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "tt", "u", "ul",
        "var", "wbr",
    }
)  # fmt: skip

# Elements that are removed together with everything inside them.
_REMOVED_WITH_CONTENT = frozenset(
    {
        "applet", "audio", "button", "canvas", "embed", "frame", "frameset", "head", "iframe",
        "math", "noembed", "noframes", "noscript", "object", "option", "plaintext", "script",
        "select", "style", "svg", "template", "textarea", "title", "video", "xmp",
    }
)  # fmt: skip

_GLOBAL_ATTRIBUTES = frozenset({"style", "class", "title", "dir", "lang", "align"})
_ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "*": _GLOBAL_ATTRIBUTES,
    "a": frozenset({"href"}),
    "img": frozenset({"src", "alt", "width", "height", "border"}),
    "font": frozenset({"color", "face", "size"}),
    "table": frozenset({"width", "height", "border", "cellpadding", "cellspacing", "bgcolor"}),
    "td": frozenset({"width", "height", "colspan", "rowspan", "valign", "bgcolor"}),
    "th": frozenset({"width", "height", "colspan", "rowspan", "valign", "bgcolor"}),
    "tr": frozenset({"valign", "bgcolor", "height"}),
    "col": frozenset({"width", "span"}),
    "colgroup": frozenset({"width", "span"}),
    "ol": frozenset({"start", "type"}),
    "ul": frozenset({"type"}),
    "hr": frozenset({"width", "size"}),
}

# CSS properties that are allowed in style attributes. None of them accepts a URL, and the
# values are additionally restricted to a syntax in which no URL can be written.
_ALLOWED_CSS_PROPERTIES = frozenset(
    {
        "background", "background-color", "border", "border-bottom", "border-bottom-color",
        "border-bottom-style", "border-bottom-width", "border-collapse", "border-color",
        "border-left", "border-left-color", "border-left-style", "border-left-width",
        "border-radius", "border-right", "border-right-color", "border-right-style",
        "border-right-width", "border-spacing", "border-style", "border-top",
        "border-top-color", "border-top-style", "border-top-width", "border-width", "clear",
        "color", "direction", "display", "float", "font", "font-family", "font-size",
        "font-style", "font-variant", "font-weight", "height", "letter-spacing", "line-height",
        "list-style-type", "margin", "margin-bottom", "margin-left", "margin-right",
        "margin-top", "max-height", "max-width", "min-height", "min-width", "padding",
        "padding-bottom", "padding-left", "padding-right", "padding-top", "table-layout",
        "text-align", "text-decoration", "text-indent", "text-transform", "vertical-align",
        "white-space", "width", "word-break", "word-wrap", "overflow-wrap",
    }
)  # fmt: skip
_CSS_VALUE = re.compile(r"[A-Za-z0-9 #%.,()+\-!/]{1,300}\Z")
_CSS_FUNCTION = re.compile(r"([A-Za-z-]*)\s*\(")
_ALLOWED_CSS_FUNCTIONS = frozenset({"rgb", "rgba", "hsl", "hsla"})

_SAFE_LINK = re.compile(r"(?:https?://|mailto:)[^\s\x00-\x1f\x7f]+\Z", re.IGNORECASE)
_DATA_IMAGE = re.compile(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}\Z")
_DIMENSION = re.compile(r"[0-9]{1,5}%?\Z")
_COLOR = re.compile(r"(?:#[0-9A-Fa-f]{3,8}|[A-Za-z]{1,30})\Z")
_FONT_FACE = re.compile(r"[A-Za-z0-9 ,_-]{1,200}\Z")
_ATTRIBUTE_VALUES = {
    "align": frozenset({"left", "right", "center", "justify", "top", "bottom", "middle"}),
    "valign": frozenset({"top", "bottom", "middle", "baseline"}),
    "dir": frozenset({"ltr", "rtl", "auto"}),
    "type": frozenset({"1", "a", "A", "i", "I", "disc", "circle", "square"}),
}
_NUMERIC_ATTRIBUTES = frozenset(
    {"width", "height", "border", "cellpadding", "cellspacing", "colspan", "rowspan", "span",
     "start"}
)  # fmt: skip

# nh3 can change or drop an attribute but cannot rename one, so a blocked image source is first
# rewritten to this sentinel and renamed afterwards. The scheme is not in the list of allowed
# URL schemes, so the sentinel cannot be smuggled in from the input.
_BLOCKED_SENTINEL = "triage-blocked:"
_BLOCKED_SERIALIZED = f' src="{_BLOCKED_SENTINEL}"'
_BLOCKED_REPLACEMENT = f' {BLOCKED_SRC_ATTRIBUTE}=""'

InlineImages = Mapping[str, tuple[str, bytes]]

_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)")
# Elements that have no content, and elements that the HTML parser closes by itself when the
# next one of their kind starts. Neither can be nested without limit on their own.
_NOT_NESTING = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr",
        "caption", "colgroup", "dd", "dt", "li", "optgroup", "option", "p", "tbody", "td",
        "tfoot", "th", "thead", "tr",
    }
)  # fmt: skip


def limit_html_depth(html: str, limit: int = MAX_HTML_DEPTH) -> tuple[str, bool]:
    """Cut HTML off at the point where its elements become nested more than ``limit`` deep.

    HTML parsers take time quadratic in the nesting depth (and some recurse), so markup that
    is nested tens of thousands of levels deep is a cheap way of stalling whoever parses it.
    The depth is estimated without parsing: an end tag only counts if it closes the element
    opened last, so the estimate errs on the high side for sloppy but harmless markup, never
    on the low side by more than a small factor. Returns the HTML and whether it was cut.
    """
    if html.count("<") <= limit:
        return html, False
    stack: list[str] = []
    for match in _TAG.finditer(html):
        name = match.group(2).lower()
        if name in _NOT_NESTING:
            continue
        if match.group(1):
            if stack and stack[-1] == name:
                stack.pop()
        else:
            stack.append(name)
            if len(stack) > limit:
                return html[: match.start()], True
    return html, False


def filter_style(value: str) -> str | None:
    """Reduce a ``style`` attribute to declarations that are known to be harmless.

    A declaration is kept only if its property is on the allowlist and its value is written in
    a restricted syntax: no quotes, no backslashes, no comments, no ``@``, and no functions
    other than colour functions. That leaves no way of writing ``url(...)`` or anything else
    that could make the browser fetch a resource.
    """
    if len(value) > 10_000:
        return None
    kept = []
    for declaration in value.split(";"):
        name, separator, raw = declaration.partition(":")
        if not separator:
            continue
        name = name.strip().lower()
        if name not in _ALLOWED_CSS_PROPERTIES:
            continue
        # Quotes are only useful around font names, which work without them.
        raw = raw.replace('"', "").replace("'", "").strip()
        raw = " ".join(raw.split())
        if not _CSS_VALUE.match(raw) or "/*" in raw or "*/" in raw:
            continue
        if any(f.lower() not in _ALLOWED_CSS_FUNCTIONS for f in _CSS_FUNCTION.findall(raw)):
            continue
        kept.append(f"{name}: {raw}")
    return "; ".join(kept) or None


def _data_uri(content_type: str, data: bytes) -> str:
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def sanitize_html(html: str, inline_images: InlineImages | None = None) -> SanitizedHtml:
    """Sanitise HTML from an email for display.

    The result contains no scripts, event handlers, forms, frames, embedded objects, or style
    sheets, and nothing that makes a browser fetch a resource: remote images are replaced by an
    ``img`` carrying the attribute ``data-triage-blocked-src``, images referenced by ``cid:``
    are inlined as ``data:`` URIs if they are in ``inline_images`` (content ID to content type
    and data), and style attributes are reduced by :func:`filter_style`. Links are kept if they
    are ``http``, ``https``, or ``mailto``; following one is up to the user.

    Every attribute passes through an allowlist with a per-attribute check on its value;
    anything that is not explicitly recognised is dropped.
    """
    images = inline_images or {}
    blocked = 0
    inlined_bytes = 0
    truncated = len(html) > MAX_HTML_CHARS
    html, too_deep = limit_html_depth(html[:MAX_HTML_CHARS])

    def image_source(value: str) -> str | None:
        nonlocal blocked, inlined_bytes
        value = value.strip()
        lowered = value[:16].lower()
        if lowered.startswith("cid:"):
            entry = images.get(value[4:].strip())
            if entry is None:
                return None
            content_type, data = entry
            if (
                content_type not in INLINE_IMAGE_TYPES
                or len(data) > MAX_INLINE_IMAGE_BYTES
                or inlined_bytes + len(data) > MAX_INLINE_IMAGES_TOTAL_BYTES
            ):
                return None
            inlined_bytes += len(data)
            return _data_uri(content_type, data)
        if lowered.startswith("data:"):
            compact = "".join(value.split())
            if len(compact) > MAX_INLINE_IMAGE_BYTES * 4 // 3 + 100:
                return None
            if not _DATA_IMAGE.match(compact):
                return None
            try:
                binascii.a2b_base64(compact.split(",", 1)[1], strict_mode=True)
            except (binascii.Error, ValueError):
                return None
            return compact
        blocked += 1
        return _BLOCKED_SENTINEL

    def attribute_filter(tag: str, attribute: str, value: str) -> str | None:
        if attribute == "style":
            return filter_style(value)
        if tag == "img" and attribute == "src":
            return image_source(value)
        if tag == "a" and attribute == "href":
            value = value.strip().replace(" ", "%20")
            if len(value) <= MAX_URL_CHARS and _SAFE_LINK.match(value):
                return value
            return None
        if attribute in ("rel", "target") and tag == "a":
            return value  # set by the sanitiser itself, see below
        if attribute in _NUMERIC_ATTRIBUTES or (tag == "hr" and attribute == "size"):
            value = value.strip()
            return value if _DIMENSION.match(value) else None
        if attribute in _ATTRIBUTE_VALUES:
            value = value.strip()
            if attribute != "type":
                value = value.lower()
            return value if value in _ATTRIBUTE_VALUES[attribute] else None
        if attribute in ("bgcolor", "color"):
            value = value.strip()
            return value if _COLOR.match(value) else None
        if tag == "font" and attribute == "size":
            value = value.strip()
            return value if re.fullmatch(r"[+-]?[0-9]", value) else None
        if attribute == "face":
            value = value.replace('"', "").replace("'", "").strip()
            return value if _FONT_FACE.match(value) else None
        if attribute in ("alt", "title", "class", "lang"):
            return value[:1000]
        return None

    cleaned = nh3.clean(
        html,
        tags=set(_ALLOWED_TAGS),
        clean_content_tags=set(_REMOVED_WITH_CONTENT),
        attributes={tag: set(names) for tag, names in _ALLOWED_ATTRIBUTES.items()},
        attribute_filter=attribute_filter,
        strip_comments=True,
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
        # The filter above has the last word on every URL. The schemes listed here are the
        # ones that it gets to see at all; nh3 drops everything else before calling it.
        url_schemes={"http", "https", "mailto", "cid", "data"},
    )
    cleaned = cleaned.replace(_BLOCKED_SERIALIZED, _BLOCKED_REPLACEMENT)
    return SanitizedHtml(clean_text(cleaned), blocked, truncated or too_deep)


# --------------------------------------------------------------------------------------------
# Quoted text

_INTRODUCERS = (
    re.compile(r"On\b.{4,400}\bwrote\s*:\s*\Z", re.IGNORECASE | re.DOTALL),
    re.compile(r"Am\b.{4,400}\bschrieb\b.{0,200}:\s*\Z", re.IGNORECASE | re.DOTALL),
    re.compile(r"Le\b.{4,400}\ba écrit\s*:\s*\Z", re.IGNORECASE | re.DOTALL),
    re.compile(r"El\b.{4,400}\bescribió\s*:\s*\Z", re.IGNORECASE | re.DOTALL),
    re.compile(r"Il\b.{4,400}\bha scritto\s*:\s*\Z", re.IGNORECASE | re.DOTALL),
    re.compile(r"Op\b.{4,400}\bschreef\b.{0,200}:\s*\Z", re.IGNORECASE | re.DOTALL),
    re.compile(r".{1,200}<[^<>\s]+@[^<>\s]+>\s+(?:wrote|schrieb|a écrit)\s*:\s*\Z", re.IGNORECASE),
)
_ORIGINAL_MESSAGE = re.compile(
    r"-{2,}\s*(?:Original Message|Ursprüngliche Nachricht|Message d'origine|Mensaje original)"
    r"\s*-{2,}\s*\Z",
    re.IGNORECASE,
)
_HEADER_BLOCK_FIRST = re.compile(r"(?:From|Von|De|Da|Van)\s*:\s*\S", re.IGNORECASE)
_HEADER_BLOCK_REST = re.compile(
    r"(?:Sent|Date|To|Cc|Subject|Gesendet|Datum|An|Betreff|Envoyé|Objet|À|Enviado|Para|Asunto"
    r"|Inviato|Oggetto|Verzonden|Aan|Onderwerp)\s*:",
    re.IGNORECASE,
)
_SIGNATURE_DELIMITER = re.compile(r"--\s?\Z")
MAX_SIGNATURE_LINES = 15


def _is_quoted(line: str) -> bool:
    return line.lstrip().startswith(">")


def _introducer_at(lines: list[str], index: int) -> int:
    """If an "On ... wrote:" line starts at ``index``, the number of lines it spans, else 0."""
    if _is_quoted(lines[index]) or not lines[index].strip():
        return 0
    # Mail clients wrap these lines, so try the line together with up to two that follow.
    for span in (1, 2, 3):
        if index + span > len(lines):
            break
        chunk = lines[index : index + span]
        if span > 1 and (not chunk[-1].strip() or _is_quoted(chunk[-1])):
            break
        joined = " ".join(part.strip() for part in chunk)
        if any(pattern.match(joined) for pattern in _INTRODUCERS):
            return span
    return 0


def _header_block_at(lines: list[str], index: int) -> bool:
    """Whether an Outlook-style block of header lines (From:/Sent:/To:/Subject:) starts here."""
    if not _HEADER_BLOCK_FIRST.match(lines[index].strip()):
        return False
    following = [line.strip() for line in lines[index + 1 : index + 6]]
    return sum(1 for line in following if _HEADER_BLOCK_REST.match(line)) >= 2


def _find_quote_start(lines: list[str]) -> int | None:
    # Index of the last line that is neither blank nor quoted: a `>` quotation only counts as
    # trailing if nothing of the author's own follows it.
    last_own = max(
        (i for i, line in enumerate(lines) if line.strip() and not _is_quoted(line)), default=-1
    )
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _is_quoted(line):
            if last_own < index:
                return index
            continue
        if len(stripped) > 1000:
            continue
        if _ORIGINAL_MESSAGE.match(stripped) or _header_block_at(lines, index):
            # Outlook separates the quote with a line of underscores; take it along.
            start = index
            while start > 0 and (
                not lines[start - 1].strip() or re.fullmatch(r"[_-]{5,}", lines[start - 1].strip())
            ):
                start -= 1
            return start
        span = _introducer_at(lines, index)
        if span:
            after = index + span
            first = next((i for i in range(after, len(lines)) if lines[i].strip()), None)
            if first is None:
                continue
            if _is_quoted(lines[first]) and last_own >= after:
                # Replies written in between the quoted lines: there is no clean split here.
                continue
            return index
    return None


def split_quoted(text: str) -> SplitText:
    """Split a top-posted reply into the new text, the signature, and the quoted remainder.

    Only a quotation that runs to the end of the message is split off. A message that replies
    in between quoted lines, or that has no recognisable quotation, is returned whole.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = _find_quote_start(lines)
    own = lines if start is None else lines[:start]
    quoted = [] if start is None else lines[start:]
    if start is not None and not any(line.strip() for line in own):
        # Nothing but a quotation (e.g. a forwarded message): do not hide all of it.
        own, quoted = lines, []

    signature: list[str] = []
    for index in range(len(own) - 1, -1, -1):
        if _SIGNATURE_DELIMITER.match(own[index]):
            candidate = own[index + 1 :]
            if any(line.strip() for line in own[:index]) and (
                sum(1 for line in candidate if line.strip()) <= MAX_SIGNATURE_LINES
            ):
                signature = candidate
                own = own[:index]
            break

    return SplitText(
        body="\n".join(own).strip("\n"),
        signature="\n".join(signature).strip("\n"),
        quoted="\n".join(quoted).strip("\n"),
    )


# --------------------------------------------------------------------------------------------
# Entry point


_INLINED_IMAGE = re.compile(r' src="data:image/[a-z]+;base64,[A-Za-z0-9+/=]*"')


def _parse_structure(raw: bytes) -> tuple[Message, list[str]]:
    if raw.count(b"\n--") > MAX_BOUNDARY_LINES:
        # Tens of thousands of MIME parts take the parser seconds, and no real message has
        # them. Do not look for parts at all.
        problem = "too many MIME boundaries; the body was not parsed"
    else:
        try:
            return email.message_from_bytes(raw, policy=email.policy.default), []
        except Exception as exc:
            problem = f"could not parse message structure ({type(exc).__name__})"
    # Fall back to parsing the headers only and treating the rest as plain text.
    try:
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            raw, headersonly=True
        )
        return message, [problem]
    except Exception:
        fallback = Message(policy=email.policy.default)
        fallback.set_payload(raw[: MAX_TEXT_CHARS * 2].decode("utf-8", "replace"))
        return fallback, [problem]


def _fill(parsed: ParsedMessage, raw: bytes) -> None:
    message, problems = _parse_structure(raw)
    parsed.problems.extend(problems)

    parsed.subject = _text_header(message, "subject", MAX_SUBJECT_CHARS)
    parsed.from_ = _address_header(message, "from")
    parsed.sender = _address_header(message, "sender")
    parsed.to = _address_header(message, "to")
    parsed.cc = _address_header(message, "cc")
    parsed.reply_to = _address_header(message, "reply-to")
    parsed.date = _date_header(message)
    parsed.message_id = next(iter(_message_ids(message, "message-id", 1)), None)
    parsed.in_reply_to = next(iter(_message_ids(message, "in-reply-to", 1)), None)
    parsed.references = _message_ids(message, "references", MAX_REFERENCES)

    walk = _Walk()
    try:
        bodies = _collect(message, 0, walk)
    except Exception as exc:
        bodies = _Bodies()
        walk.problems.append(f"could not read message body ({type(exc).__name__})")
    parsed.attachments = walk.attachments
    parsed.problems.extend(walk.problems)

    html_source = "\n".join(part for part in bodies.html if part.strip())
    plain = "\n\n".join(part.strip("\n") for part in bodies.plain if part.strip())
    if html_source:
        try:
            sanitized = sanitize_html(html_source, walk.inline_images)
            parsed.html = sanitized.html
            parsed.blocked_remote_content = sanitized.blocked_remote_content
            if sanitized.truncated:
                parsed.problems.append("HTML body too large or too deeply nested; truncated")
        except Exception as exc:
            parsed.problems.append(f"could not sanitise HTML ({type(exc).__name__})")
    if not plain and html_source:
        # Prefer the sanitised markup as the source: it is well-formed, which keeps the text
        # extraction fast even when the original was written to be pathological.
        # Inlined images are left out: they are large, and there is nothing to read in them.
        plain = html_to_text(
            _INLINED_IMAGE.sub("", parsed.html) if parsed.html is not None else html_source
        )
    parsed.text = clean_text(plain)[:MAX_TEXT_CHARS]


def parse_message(raw: bytes) -> ParsedMessage:
    """Parse a raw RFC 5322 message. Never raises, whatever the input."""
    parsed = ParsedMessage()
    try:
        _fill(parsed, bytes(raw))
    except Exception as exc:  # the safety net below the safety nets
        parsed.problems.append(f"parser failed ({type(exc).__name__})")
    return parsed

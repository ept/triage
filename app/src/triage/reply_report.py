"""Describe how the user's mail program lays out replies, without revealing any mail.

Run inside the email sandbox, over the local copy of the user's sent mail::

    python -m triage.reply_report

The purpose is to let the developers of this app (who must never see the user's mail) make the
app's drafts look like the replies that the user's own mail program writes.

**What the output may contain** is deliberately narrow, so that it is safe to share:

* counts;
* names of MIME types, of their parameters, and of header fields (never a header's value,
  except the version of the mail program and the encoding labels of a body part);
* the names of HTML elements and attributes, and the values of a few layout attributes
  (``type``, ``dir``, the character set declared by a ``meta`` element, classes that mail
  programs are known to write, and in a ``style`` the settings that are plain keywords or
  numbers);
* the *shape* of a line that introduces a quotation: digits become ``9``, addresses become
  ``ADDR``, and every word becomes ``Xx``, ``xx`` or ``XX`` unless it is one of the fixed words
  in :data:`KEPT_WORDS` (``On``, ``at``, ``wrote``, names of months and the like).

Nothing else of a message is printed: no text, name, address, date, subject or folder name.
Keep it that way when changing this module; ``tests/test_reply_report.py`` checks it.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from email.message import Message
from html.parser import HTMLParser

from triage import mime
from triage.reply import find_attribution
from triage.store import Store

__all__ = ["KEPT_WORDS", "Report", "main", "report", "shape"]

KEPT_WORDS = frozenset(
    [
        "On",
        "at",
        "wrote",
        "AM",
        "PM",
        "am",
        "pm",
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Sept",
        "Oct",
        "Nov",
        "Dec",
        "January",
        "February",
        "March",
        "April",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat",
        "Sun",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "Am",
        "um",
        "schrieb",
        "Le",
        "à",
        "a",
        "écrit",
        "El",
        "escribió",
        "Il",
        "giorno",
        "alle",
        "ore",
        "ha",
        "scritto",
        "Op",
        "om",
        "schreef",
    ]
)
_MONTH_WORDS = frozenset(
    [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Sept",
        "Oct",
        "Nov",
        "Dec",
        "January",
        "February",
        "March",
        "April",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
)

_ADDRESS = re.compile(r"<[^<>]*>|\S*@\S*")
_WORD = re.compile(r"[^\W\d_]+")
_QUOTE_PREFIX = re.compile(r"\A(?:>[ \t]?)*")
_MAILER = re.compile(r"\A(Apple Mail|iPhone Mail|iPad Mail) \(([0-9A-Za-z.]{1,24})\)\Z")
_LABEL = re.compile(r"\A[a-z0-9_.+-]{1,24}\Z")
_HEADER_NAME = re.compile(r"\A[a-z0-9-]{1,40}\Z")
_ATTRIBUTE_NAME = re.compile(r"\A[a-z0-9:_-]{1,30}\Z")
_TOKEN_VALUE = re.compile(r"\A[A-Za-z0-9/_.=; -]{0,60}\Z")
_KNOWN_CLASS = re.compile(
    r"\A(?:Apple|apple|gmail|Mso|mso|moz|yahoo|WordSection)[A-Za-z0-9_-]{0,40}\Z"
)
_STYLE_NAME = re.compile(r"\A[a-z-]{1,40}\Z")
# Lower case only: keywords, numbers and units, but not the name of a font or anything else
# that somebody chose.
_STYLE_VALUE = re.compile(r"\A(?!.*url)[a-z0-9 .,%#()-]{1,60}\Z")
_VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)
_ENCODINGS = frozenset({"7bit", "8bit", "binary", "quoted-printable", "base64"})
MAX_HTML_NODES = 20_000


def shape(line: str) -> str:
    """The shape of a line: what kind of thing stands where, but not what it says."""
    text = _ADDRESS.sub(lambda match: "<ADDR>" if match[0].startswith("<") else "ADDR", line)

    def word(match: re.Match[str]) -> str:
        found = match[0]
        if found in KEPT_WORDS or found == "ADDR":
            return found
        if found.isupper() and len(found) > 1:
            return "XX"
        return "Xx" if found[0].isupper() else "xx"

    text = _WORD.sub(word, text)
    text = re.sub(r"\d", "9", text)
    # Whatever is left is punctuation and spacing. Unusual spaces are shown by their number,
    # as they are part of how a mail program writes a date; other symbols are not shown.
    return "".join(map(_plain, text))[:160]


def _plain(char: str) -> str:
    if char.isascii():
        return char if char.isprintable() else "?"
    if unicodedata.category(char) in ("Zs", "Zl", "Zp", "Cf"):
        return f"[U+{ord(char):04X}]"
    return "?"


def _spaces(text: str) -> str:
    return "".join(map(_plain, text))


# --------------------------------------------------------------------------------------------
# HTML


@dataclass(slots=True)
class _Node:
    tag: str
    attributes: list[tuple[str, str | None]] = field(default_factory=list)
    children: list[_Node] = field(default_factory=list)
    parent: _Node | None = None


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document")
        self._current = self.root
        self._count = 0

    def _add(self, node: _Node) -> bool:
        self._count += 1
        if self._count > MAX_HTML_NODES:
            return False
        node.parent = self._current
        self._current.children.append(node)
        return True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, attrs)
        if self._add(node) and tag not in _VOID:
            self._current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._add(_Node(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        node: _Node | None = self._current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self._current = node.parent

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._add(_Node("#text"))


def _shown(tag: str, name: str, value: str, attributes: Sequence[tuple[str, str | None]]) -> bool:
    """Whether the value of an attribute says something about layout, and nothing else."""
    if name in ("type", "dir", "charset", "http-equiv"):
        return _TOKEN_VALUE.match(value) is not None
    if name == "content":
        # Only the declaration of the character set; other `meta` elements can name people.
        return (
            tag == "meta"
            and any(other == "http-equiv" for other, _ in attributes)
            and _TOKEN_VALUE.match(value) is not None
        )
    if name == "class":
        return all(_KNOWN_CLASS.match(token) for token in value.split())
    return False


def _style(value: str) -> str:
    """The properties of a style, with those values that are plain keywords or numbers."""
    shown = []
    for declaration in value.split(";")[:12]:
        name, colon, setting = declaration.partition(":")
        name, setting = name.strip().lower(), setting.strip()
        if not colon or not _STYLE_NAME.match(name):
            continue
        shown.append(f"{name}: {setting}" if _STYLE_VALUE.match(setting) else f"{name}: …")
    return "; ".join(shown)


def _describe(node: _Node) -> str:
    if node.tag.startswith("#"):
        return node.tag
    tag = node.tag if _ATTRIBUTE_NAME.match(node.tag) else "?"
    shown = []
    for name, value in node.attributes:
        if not _ATTRIBUTE_NAME.match(name):
            shown.append("?")
        elif name == "style" and value is not None:
            shown.append(f'style="{_style(value)}"')
        elif value is not None and _shown(tag, name, value, node.attributes):
            shown.append(f'{name}="{value}"')
        else:
            shown.append(name)
    return f"{tag}[{', '.join(shown)}]" if shown else tag


def _collapsed(descriptions: Iterable[str]) -> list[str]:
    lines: list[tuple[str, int]] = []
    for description in descriptions:
        if lines and lines[-1][0] == description:
            lines[-1] = (description, lines[-1][1] + 1)
        else:
            lines.append((description, 1))
    return [text if count == 1 else f"{text} ×{count}" for text, count in lines]


def _is_cite(node: _Node) -> bool:
    return node.tag == "blockquote" and any(
        name == "type" and (value or "").lower() == "cite" for name, value in node.attributes
    )


def _first(node: _Node, wanted: str) -> _Node | None:
    stack = [node]
    while stack:
        current = stack.pop()
        if (wanted == "cite" and _is_cite(current)) or current.tag == wanted:
            return current
        stack.extend(reversed(current.children))
    return None


def html_skeleton(html: str) -> str:
    """The elements around the first quotation of an HTML reply, without any of its text."""
    parser = _Tree()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return "(the HTML could not be read)"
    target = _first(parser.root, "cite")
    note = ""
    if target is None:
        target = _first(parser.root, "blockquote")
        note = "(no blockquote with type=cite; showing the first blockquote)\n"
    if target is None:
        return "(no blockquote)"
    path: list[_Node] = []
    node: _Node | None = target
    while node is not None and node.parent is not None:
        path.append(node)
        node = node.parent
    path.reverse()

    lines: list[str] = []
    for depth, element in enumerate(path):
        indent = "  " * depth
        siblings = element.parent.children if element.parent is not None else [element]
        position = next(i for i, sibling in enumerate(siblings) if sibling is element)
        lines += [f"{indent}{text}" for text in _collapsed(map(_describe, siblings[:position]))]
        lines.append(
            f"{indent}{_describe(element)}" + ("   <-- the quotation" * (element is target))
        )
    inner = "  " * len(path)
    for child in target.children[:4]:
        inside = _collapsed(map(_describe, child.children[:4]))
        more = " …" if len(child.children) > 4 else ""
        lines.append(
            f"{inner}{_describe(child)}" + (f" > {', '.join(inside)}{more}" * bool(inside))
        )
    if len(target.children) > 4:
        lines.append(f"{inner}…")
    # What follows the quotation, from the inside out.
    for depth in range(len(path) - 1, -1, -1):
        element = path[depth]
        siblings = element.parent.children if element.parent is not None else [element]
        position = next(i for i, sibling in enumerate(siblings) if sibling is element)
        after = _collapsed(map(_describe, siblings[position + 1 :]))
        lines += [f"{'  ' * depth}{text}   (after)" for text in after[:6]]
    return note + "\n".join(lines)


# --------------------------------------------------------------------------------------------
# MIME


def _kind(part: Message) -> str:
    content_type = mime._content_type(part)
    main = content_type.split("/", 1)[0]
    if main in ("text", "multipart"):
        return content_type if _LABEL.match(content_type.replace("/", ".")) else f"{main}/?"
    return f"{main}/*" if main in ("image", "application", "audio", "video", "message") else "?/*"


def structure(part: Message, depth: int = 0) -> str:
    children = mime._children(part)
    if not children or depth > 6:
        return _kind(part)
    return f"{_kind(part)}({', '.join(structure(child, depth + 1) for child in children)})"


def _label(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if _LABEL.match(text) else ("(none)" if not text else "(other)")


def _encoding_of(part: Message) -> str:
    value = _label(mime._safe(lambda: part.get("Content-Transfer-Encoding", ""), ""))
    return value if value in _ENCODINGS or value == "(none)" else "(other)"


def _parameters(part: Message) -> str:
    found = []
    for name in ("charset", "format", "delsp"):
        value = _parameter(part, name)
        if value is not None:
            found.append(f"{name}={_label(value)}")
    return "; ".join(found) or "(none)"


def _parameter(part: Message, name: str) -> object:
    return mime._safe(lambda: part.get_param(name), None)


def _first_part(part: Message, content_type: str, depth: int = 0) -> Message | None:
    children = mime._children(part)
    if not children:
        return part if mime._content_type(part) == content_type else None
    if depth > 6:
        return None
    for child in children:
        found = _first_part(child, content_type, depth + 1)
        if found is not None:
            return found
    return None


def _raw_text(part: Message) -> str:
    """The text of a part as it was sent: decoded, but with its line breaks left alone."""
    data = mime._payload_bytes(part)[:2_000_000]
    charset = mime._safe(part.get_content_charset, None)
    text = mime.decode_bytes(data, charset if isinstance(charset, str) else None)
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------------------------
# The report


@dataclass(slots=True)
class Report:
    messages: int = 0
    mailers: Counter[str] = field(default_factory=Counter)
    replies: int = 0
    structures: Counter[str] = field(default_factory=Counter)
    plain_parameters: Counter[str] = field(default_factory=Counter)
    plain_encodings: Counter[str] = field(default_factory=Counter)
    html_parameters: Counter[str] = field(default_factory=Counter)
    html_encodings: Counter[str] = field(default_factory=Counter)
    header_names: Counter[str] = field(default_factory=Counter)
    recognised: Counter[str] = field(default_factory=Counter)
    unrecognised: Counter[str] = field(default_factory=Counter)
    no_introduction: int = 0
    months: Counter[str] = field(default_factory=Counter)
    padded_hours: Counter[str] = field(default_factory=Counter)
    who: Counter[str] = field(default_factory=Counter)
    depth: Counter[str] = field(default_factory=Counter)
    blank_before: Counter[str] = field(default_factory=Counter)
    line_after: Counter[str] = field(default_factory=Counter)
    after_quote: Counter[str] = field(default_factory=Counter)
    prefixes: Counter[str] = field(default_factory=Counter)
    empty_prefixes: Counter[str] = field(default_factory=Counter)
    longest_line: Counter[str] = field(default_factory=Counter)
    skeletons: Counter[str] = field(default_factory=Counter)
    draft_messages: int = 0
    draft_header_names: Counter[str] = field(default_factory=Counter)

    # -- collecting

    def add_draft(self, raw: bytes) -> None:
        message, _ = mime._parse_structure(raw)
        self.draft_messages += 1
        self.draft_header_names.update(_header_names(message))

    def add_sent(self, raw: bytes) -> None:
        message, _ = mime._parse_structure(raw)
        self.messages += 1
        mailer = _MAILER.match(str(mime._safe(lambda: message.get("X-Mailer", ""), "")).strip())
        self.mailers[
            f"{mailer[1]} ({mailer[2]})" if mailer else "another program, or not stated"
        ] += 1
        if mailer is None or mailer[1] != "Apple Mail":
            return
        if not mime._safe(lambda: message.get("In-Reply-To"), None):
            return
        self.replies += 1
        self.structures[structure(message)] += 1
        self.header_names.update(_header_names(message))
        plain = _first_part(message, "text/plain")
        if plain is not None:
            self.plain_parameters[_parameters(plain)] += 1
            self.plain_encodings[_encoding_of(plain)] += 1
            self._add_plain(_raw_text(plain))
        html = _first_part(message, "text/html")
        if html is not None:
            self.html_parameters[_parameters(html)] += 1
            self.html_encodings[_encoding_of(html)] += 1
            self.skeletons[html_skeleton(_raw_text(html))] += 1

    def _add_plain(self, text: str) -> None:
        lines = text.split("\n")
        longest = max((len(line) for line in lines), default=0)
        self.longest_line["at most 78 characters" if longest <= 78 else "longer"] += 1
        for line in lines:
            prefix = _QUOTE_PREFIX.match(line)
            if prefix is None or not prefix[0]:
                continue
            counter = self.prefixes if line[len(prefix[0]) :].strip() else self.empty_prefixes
            counter[repr(_spaces(prefix[0].replace("\t", " "))[:12])] += 1

        index = next((i for i, line in enumerate(lines) if find_attribution(line)), None)
        if index is None:
            # Not a form that is known: show the shape of what stands in its place.
            first_quoted = next((i for i, line in enumerate(lines) if line.startswith(">")), None)
            candidates = [i for i, line in enumerate(lines) if line.rstrip().endswith("wrote:")]
            if candidates:
                index = candidates[0]
            elif first_quoted is not None:
                above = [i for i in range(first_quoted) if lines[i].strip()]
                index = above[-1] if above and lines[above[-1]].rstrip().endswith(":") else None
            if index is None:
                self.no_introduction += 1
                return
            self.unrecognised[shape(_QUOTE_PREFIX.sub("", lines[index], count=1))] += 1
        else:
            found = find_attribution(lines[index])
            assert found is not None
            clock = f"12-hour clock, {found.half_day!r} after {_spaces(found.half_day_gap)!r}"
            order = "day first" if found.day_first else "month first"
            self.recognised[f"{order}, {clock if found.half_day else '24-hour clock'}"] += 1
            self.months[found.month_name if found.month_name in _MONTH_WORDS else "(other)"] += 1
            if found.hour < 10:
                self.padded_hours["09" if found.hour_text.startswith("0") else "9"] += 1
            self.who[shape(found.who)] += 1
        prefix_match = _QUOTE_PREFIX.match(lines[index])
        self.depth[str(prefix_match[0].count(">") if prefix_match else 0)] += 1
        blank = 0
        while index - blank - 1 >= 0 and not lines[index - blank - 1].strip():
            blank += 1
        self.blank_before["start of the message" if blank == index else str(blank)] += 1
        following = lines[index + 1] if index + 1 < len(lines) else None
        self.line_after[_line_kind(following)] += 1
        rest = lines[index + 1 :]
        own_after = any(line.strip() and not line.startswith(">") for line in rest)
        self.after_quote["text of the user's own follows" if own_after else "nothing"] += 1

    # -- printing

    def text(self) -> str:
        out: list[str] = [
            "How replies in the sent mail are laid out",
            "=========================================",
            "This describes structure only. It contains no text, name, address, date, subject",
            "or folder name from any message: words are shown as Xx, digits as 9, addresses",
            "as ADDR. Please look through it before you pass it on.",
            "",
            f"Sent messages looked at: {self.messages}",
        ]
        out += _table("Written by", self.mailers)
        out.append(f"Replies written by Apple Mail on a Mac, described below: {self.replies}")
        out += _table("Structure", self.structures)
        out += _table("text/plain: parameters", self.plain_parameters)
        out += _table("text/plain: transfer encoding", self.plain_encodings)
        out += _table("text/plain: longest line", self.longest_line)
        out += _table("text/html: parameters", self.html_parameters)
        out += _table("text/html: transfer encoding", self.html_encodings)
        out += _table("Header fields (names only)", self.header_names, limit=40)
        out += _table("The line that introduces the quotation: known forms", self.recognised)
        out += _table("... other forms (shape)", self.unrecognised, limit=12)
        out.append(f"... replies with no such line: {self.no_introduction}")
        out += _table("... names of months as written", self.months, limit=24)
        out += _table("... hours before 10 are written as", self.padded_hours)
        out += _table("... who wrote (shape)", self.who, limit=8)
        out += _table("... number of quote marks in front of that line", self.depth)
        out += _table("... empty lines above it", self.blank_before)
        out += _table("... the line below it", self.line_after)
        out += _table("... after the quotation", self.after_quote)
        out += _table("Quote marks in front of quoted text", self.prefixes, limit=8)
        out += _table("Quote marks on empty quoted lines", self.empty_prefixes, limit=8)
        out += ["", f"HTML around the quotation ({len(self.skeletons)} different forms)"]
        for skeleton, count in self.skeletons.most_common(4):
            out += [f"  --- ×{count}", *(f"  {line}" for line in skeleton.split("\n"))]
        out += ["", f"Messages in the drafts folders: {self.draft_messages}"]
        out += _table("Header fields of drafts (names only)", self.draft_header_names, limit=40)
        return "\n".join(out) + "\n"


def _header_names(message: Message) -> set[str]:
    empty: list[str] = []
    names = {str(name).lower() for name in mime._safe(message.keys, empty)}
    return {name if _HEADER_NAME.match(name) else "(other)" for name in names}


def _line_kind(line: str | None) -> str:
    if line is None:
        return "nothing (end of the message)"
    if not line.strip():
        return "an empty line without a quote mark"
    prefix = _QUOTE_PREFIX.match(line)
    marks = prefix[0] if prefix else ""
    if not marks:
        return "text without a quote mark"
    if line[len(marks) :].strip():
        return "quoted text"
    return f"an empty quoted line, written {_spaces(marks)[:12]!r}"


def _table(title: str, counts: Counter[str], limit: int = 10) -> list[str]:
    lines = ["", f"{title}:"]
    common = counts.most_common(limit)
    lines += [f"  {count:5d}  {name}" for name, count in common] or ["      -"]
    if len(counts) > limit:
        rest = sum(counts.values()) - sum(count for _, count in common)
        lines.append(f"  {rest:5d}  in {len(counts) - limit} further forms")
    return lines


def report(store: Store, limit: int = 500) -> Report:
    """Look at the newest ``limit`` messages of every sent folder (and 50 of every drafts
    folder) in the local copy of the mail."""
    result = Report()
    for folder in store.folders():
        if folder.role not in ("sent", "drafts"):
            continue
        newest = limit if folder.role == "sent" else min(limit, 50)
        for summary in store.list_folder(folder.account, folder.name, limit=newest):
            raw = store.get_raw(summary.key)
            if raw is None:
                continue
            try:
                if folder.role == "sent":
                    result.add_sent(raw)
                else:
                    result.add_draft(raw)
            except Exception:  # noqa: S112 - one odd message must not stop the report
                continue
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m triage.reply_report", description=__doc__)
    parser.add_argument("--limit", type=int, default=500, help="messages per sent folder")
    parser.add_argument("--cache-dir", default=None, help="where the local copy of the mail is")
    arguments = parser.parse_args(argv)
    with Store(arguments.cache_dir) as store:
        sys.stdout.write(report(store, max(1, arguments.limit)).text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

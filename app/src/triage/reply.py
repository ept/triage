"""The layout of a reply: the new text on top, the message replied to quoted below.

The layout follows what Apple Mail writes when it sends a reply as plain text::

    The reply.

    > On 27 Nov 2026, at 10:15, Alice Example <alice@example.org> wrote:
    >
    > What Alice wrote.
    >> What Alice had quoted.

(An empty quoted line ends with a space: ``"> "``, ``">> "``.) This has been compared, with
``triage.reply_report``, with several hundred replies written by Mail 16 on a Mac.

How the date in the first quoted line is written depends on the settings of the user's Mac,
which the email sandbox cannot see. It is therefore learnt from the replies in the user's own
sent mail (:func:`learn_style`).

Drafts are plain text (the gateway accepts nothing else), so a message that only has an HTML
body is quoted as its text.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

from triage.mime import Address
from triage.store import Store

__all__ = [
    "MAX_DRAFT_CHARS",
    "Attribution",
    "ReplyStyle",
    "attribution_line",
    "find_attribution",
    "learn_style",
    "quote",
    "style_of_account",
    "time_zone",
    "visible",
    "with_quote",
]

MAX_DRAFT_CHARS = 100_000
"""The gateway's limit on the body of a draft."""

CUT_SHORT = "[The rest of the message is not quoted here.]"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_NUMBER = {name.lower(): number for number, name in enumerate(_MONTHS, start=1)}

# The space before "AM" is a narrow no-break space in recent versions of macOS.
_TIME = r"(?P<hour>\d{1,2})[:.](?P<minute>\d{2})(?:(?P<gap>[   ]?)(?P<half>[AaPp]\.?[Mm]\.?))?"
_MONTH = r"(?P<month>[A-Za-z]{3,9})\.?"
_WHO = r"(?P<who>.{1,400}?)"
_DAY_FIRST = re.compile(
    rf"On (?P<day>\d{{1,2}}) {_MONTH} (?P<year>\d{{4}}), at {_TIME}, {_WHO} wrote:\s*\Z"
)
_MONTH_FIRST = re.compile(
    rf"On {_MONTH} (?P<day>\d{{1,2}}), (?P<year>\d{{4}}), at {_TIME}, {_WHO} wrote:\s*\Z"
)
_QUOTE_PREFIX = re.compile(r"\A(?:>[ \t]?)*")


@dataclass(frozen=True, slots=True)
class ReplyStyle:
    """How the user's mail program writes the line that introduces a quotation."""

    day_first: bool = True
    """``27 Nov 2026`` rather than ``Nov 27, 2026``."""
    half_day: str = ""
    """The marker of a 12-hour clock as written for the morning (``AM``, ``am``), or empty for
    a 24-hour clock."""
    half_day_gap: str = " "
    """What stands between the time and that marker."""
    months: tuple[str, ...] = _MONTHS
    """The names of the months as they are written (some systems write ``Sept``)."""


@dataclass(frozen=True, slots=True)
class Attribution:
    """An introducing line found in an existing message."""

    day_first: bool
    month: int
    month_name: str
    hour: int
    hour_text: str
    half_day: str
    half_day_gap: str
    who: str


def find_attribution(line: str) -> Attribution | None:
    """Recognise a line such as ``> On 27 Nov 2026, at 10:15, A <a@example.org> wrote:``."""
    text = _QUOTE_PREFIX.sub("", line.strip(), count=1).strip()
    if len(text) > 600:
        return None
    for pattern, day_first in ((_DAY_FIRST, True), (_MONTH_FIRST, False)):
        match = pattern.match(text)
        if match is None:
            continue
        month = _MONTH_NUMBER.get(match["month"][:3].lower())
        if month is None or not 1 <= int(match["day"]) <= 31 or int(match["hour"]) > 23:
            continue
        return Attribution(
            day_first=day_first,
            month=month,
            month_name=match["month"],
            hour=int(match["hour"]),
            hour_text=match["hour"],
            half_day=match["half"] or "",
            half_day_gap=match["gap"] or "",
            who=match["who"],
        )
    return None


def _morning(marker: str) -> str:
    """``PM`` -> ``AM``, ``p.m.`` -> ``a.m.``: the marker as it is written before noon."""
    return marker.translate(str.maketrans("Pp", "Aa"))


def _afternoon(marker: str) -> str:
    return marker.translate(str.maketrans("Aa", "Pp"))


@dataclass(slots=True)
class _Tally:
    day_first: Counter[bool] = field(default_factory=Counter)
    half_day: Counter[tuple[str, str]] = field(default_factory=Counter)
    months: dict[int, Counter[str]] = field(default_factory=dict)


def learn_style(texts: Iterable[str]) -> ReplyStyle | None:
    """Work out the style from the text of messages that the user sent.

    Returns None if no reply among them has a line that is recognised.
    """
    tally = _Tally()
    for text in texts:
        for line in text.splitlines():
            if "wrote:" not in line:
                continue
            found = find_attribution(line)
            if found is None:
                continue
            tally.day_first[found.day_first] += 1
            tally.half_day[(_morning(found.half_day), found.half_day_gap)] += 1
            tally.months.setdefault(found.month, Counter())[found.month_name] += 1
            # A reply quotes the message before it, which quotes the one before that; only the
            # outermost line was written by the user's own mail program.
            break
    if not tally.day_first:
        return None
    half_day, gap = tally.half_day.most_common(1)[0][0]
    months = tuple(
        tally.months[number].most_common(1)[0][0] if number in tally.months else default
        for number, default in enumerate(_MONTHS, start=1)
    )
    return ReplyStyle(
        day_first=tally.day_first.most_common(1)[0][0],
        half_day=half_day,
        half_day_gap=gap if half_day else " ",
        months=months,
    )


def style_of_account(store: Store, account: str, sample: int = 40) -> ReplyStyle | None:
    """Learn the style from the newest replies in the sent mail of an account."""
    texts: list[str] = []
    for folder in store.folders(account):
        if folder.role != "sent":
            continue
        for summary in store.search(
            "wrote", account=account, folder=folder.name, order="date", limit=sample
        ):
            message = store.get_message(summary.key)
            if message is not None:
                texts.append(message.body)
    return learn_style(texts)


def time_zone(name: str) -> tzinfo:
    """The time zone with this name (such as ``Europe/London``), or UTC if there is none."""
    if name and re.fullmatch(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,2}", name):
        try:
            return ZoneInfo(name)
        except (OSError, ValueError, KeyError):  # an unknown name is a KeyError
            pass
    return UTC


def attribution_line(
    sender: Address | None, when: datetime, zone: tzinfo, style: ReplyStyle | None = None
) -> str:
    """The line that introduces the quotation, without a quote mark."""
    style = style or ReplyStyle()
    local = when.astimezone(zone)
    month = style.months[local.month - 1]
    if style.day_first:
        date = f"{local.day} {month} {local.year}"
    else:
        date = f"{month} {local.day}, {local.year}"
    if style.half_day:
        marker = style.half_day if local.hour < 12 else _afternoon(style.half_day)
        time = f"{local.hour % 12 or 12}:{local.minute:02d}{style.half_day_gap}{marker}"
    else:
        time = f"{local.hour:02d}:{local.minute:02d}"
    who = " ".join(visible(str(sender)).split()) if sender is not None else ""
    return f"On {date}, at {time}, {who} wrote:" if who else f"On {date}, at {time}, wrote:"


# The characters that the gateway refuses in a draft because they cannot be seen (its
# `rules::is_invisible`). Marketing mail is full of them, and a quotation must not make the
# whole draft unacceptable.
_INVISIBLE = re.compile(
    "[­͏؜ᅟᅠ឴឵᠋-᠏​-‏ -‮"
    "⁠-⁯ㅤ︀-️﻿ﾠ￰-￻\U0001bca0-\U0001bca3"
    "\U0001d173-\U0001d17a-\U000e0000-\U000e0fff\U000f0000-\U0010ffff]"
)
_TOLERATED_JOINERS = frozenset("‌‍️")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def visible(text: str) -> str:
    """Remove what the gateway does not accept in the body of a draft.

    Joiners that some scripts and emoji need are kept, as the gateway allows them, unless they
    follow another invisible character. Line and paragraph separators become line breaks.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", "\n").replace(" ", "\n")
    text = _CONTROL.sub("", text)
    if not _INVISIBLE.search(text):
        return text
    kept: list[str] = []
    previous_invisible = False
    for char in text:
        invisible = _INVISIBLE.match(char) is not None
        if not invisible or (char in _TOLERATED_JOINERS and not previous_invisible):
            kept.append(char)
        # As in the gateway, a joiner that is let through still counts for the next one.
        previous_invisible = invisible
    return "".join(kept)


def quote(text: str) -> str:
    """Put a quote mark in front of every line, as a mail program does when replying."""
    return "\n".join(map(_quoted, visible(text).strip("\n").split("\n")))


def _quoted(line: str) -> str:
    line = line.rstrip()
    if not line.startswith(">"):
        return f"> {line}"
    # A line that was quoted before gets one more mark, directly in front of the others.
    # Apple Mail ends an empty quoted line with a space at every depth ("> ", ">> ").
    return f">{line} " if set(line) == {">"} else f">{line}"


def with_quote(reply: str, introduction: str, original: str, limit: int = MAX_DRAFT_CHARS) -> str:
    """The body of a reply: ``reply``, then ``original`` quoted below ``introduction``.

    If the result would be longer than ``limit`` characters, the end of the quotation is left
    out and a quoted note says so. The reply itself is never shortened.
    """
    top = f"{reply.strip()}\n\n> {introduction}\n> \n"
    quoted = quote(original)
    if len(top) + len(quoted) <= limit:
        return top + quoted
    note = f"> {CUT_SHORT}"
    room = limit - len(top) - len(note) - len("\n> \n")
    if len(top) + len(note) > limit:
        return reply.strip()
    # End at a line break, so that no line (and no quote mark) is cut in half.
    cut = quoted[: max(room, 0)]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else ""
    return f"{top}{cut}\n> \n{note}" if cut else top + note

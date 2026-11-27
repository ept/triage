"""The layout of replies: the introducing line, the quotation, and learning from sent mail."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from triage.mime import Address
from triage.reply import (
    CUT_SHORT,
    ReplyStyle,
    attribution_line,
    find_attribution,
    learn_style,
    quote,
    time_zone,
    visible,
    with_quote,
)

ALICE = Address("Alice Example", "alice@example.org")
WHEN = datetime(2026, 9, 3, 13, 5, tzinfo=UTC)


def test_the_introducing_line() -> None:
    line = attribution_line(ALICE, WHEN, UTC)
    assert line == "On 3 Sep 2026, at 13:05, Alice Example <alice@example.org> wrote:"
    london = attribution_line(ALICE, WHEN, time_zone("Europe/London"))
    assert london == "On 3 Sep 2026, at 14:05, Alice Example <alice@example.org> wrote:"
    american = ReplyStyle(day_first=False, half_day="AM", half_day_gap="\u202f")
    assert (
        attribution_line(ALICE, WHEN, time_zone("America/New_York"), american)
        == "On Sep 3, 2026, at 9:05\u202fAM, Alice Example <alice@example.org> wrote:"
    )
    assert attribution_line(Address("", "a@example.org"), WHEN, UTC, american) == (
        "On Sep 3, 2026, at 1:05\u202fPM, a@example.org wrote:"
    )
    midnight = datetime(2026, 1, 1, 0, 7, tzinfo=UTC)
    assert "at 12:07 am," in attribution_line(ALICE, midnight, UTC, ReplyStyle(half_day="am"))
    assert "at 00:07," in attribution_line(ALICE, midnight, UTC)
    # A name cannot break the line or smuggle in something invisible.
    odd = Address("Eve\nExample\u200b", "eve@example.org")
    assert attribution_line(odd, WHEN, UTC).endswith(", Eve Example <eve@example.org> wrote:")


@pytest.mark.parametrize("name", ["", "Nowhere/Special", "../../etc/passwd", "x" * 200])
def test_an_unknown_time_zone_means_utc(name: str) -> None:
    assert time_zone(name) is UTC


@pytest.mark.parametrize(
    ("line", "day_first", "half_day"),
    [
        ("On 27 Nov 2026, at 10:15, Alice Example <alice@example.org> wrote:", True, ""),
        ("> On 3 Sept 2026, at 09:05, alice@example.org wrote:", True, ""),
        (">> On Nov 27, 2026, at 10:15 AM, Alice Example <alice@example.org> wrote:", False, "AM"),
        ("On Nov 7, 2026, at 4:15\u202fPM, Alice <alice@example.org> wrote:  ", False, "PM"),
        ("On 7 Nov 2026, at 4:15 pm, Alice <alice@example.org> wrote:", True, "pm"),
    ],
)
def test_recognising_the_line(line: str, day_first: bool, half_day: str) -> None:
    found = find_attribution(line)
    assert found is not None
    assert (found.day_first, found.half_day) == (day_first, half_day)
    assert found.month in (9, 11)


@pytest.mark.parametrize(
    "line",
    [
        "On Thursday Alice wrote:",
        "Alice Example <alice@example.org> wrote:",
        "On 27 Foo 2026, at 10:15, Alice wrote:",
        "On 27 Nov 2026, at 10:15, Alice wrote: and then some",
        "Am 27.11.2026 um 10:15 schrieb Alice:",
        "On 27 Nov 2026, at 10:15, " + "x" * 700 + " wrote:",
    ],
)
def test_other_lines_are_not_recognised(line: str) -> None:
    assert find_attribution(line) is None


def test_learning_the_style_from_sent_mail() -> None:
    assert learn_style([]) is None
    assert learn_style(["No quotation here.", "She wrote: hello"]) is None
    british = "Fine.\n\n> On 3 Sept 2026, at 09:05, Alice <alice@example.org> wrote:\n> \n> Hi\n"
    american = "OK\n\n> On Nov 2, 2026, at 9:05\u202fPM, Bob <bob@example.org> wrote:\n> \n> Hi\n"
    style = learn_style([british, british, american])
    assert style is not None
    assert (style.day_first, style.half_day) == (True, "")
    assert style.months[8] == "Sept", "the month is written as the user's system writes it"
    assert style.months[0] == "Jan"
    style = learn_style([american, american, british])
    assert style == ReplyStyle(
        day_first=False,
        half_day="AM",
        half_day_gap="\u202f",
        months=(*ReplyStyle().months[:8], "Sept", *ReplyStyle().months[9:]),
    )
    # Only the outermost introducing line of a message is the user's own mail program's.
    nested = (
        "Yes\n\n> On 5 Nov 2026, at 10:00, A <a@example.org> wrote:\n> \n> ok\n"
        ">> On Nov 4, 2026, at 9:00 AM, B <b@example.org> wrote:\n"
        ">>> On Nov 3, 2026, at 8:00 AM, C <c@example.org> wrote:\n"
    )
    learnt = learn_style([nested])
    assert learnt is not None
    assert learnt.day_first
    assert learnt.half_day == ""


def test_quoting() -> None:
    assert quote("one\n\ntwo  \n> older\n>> oldest\n") == ("> one\n> \n> two\n>> older\n>>> oldest")
    assert quote("\r\nwindows\r\nlines\r\n") == "> windows\n> lines"
    # Empty quoted lines end with a space at every depth, however the original wrote them.
    assert quote("a\n> \n>\n>>\n>> b") == "> a\n>> \n>> \n>>> \n>>> b"
    assert quote("") == "> "


def test_what_the_gateway_would_refuse_is_removed() -> None:
    assert visible("a\u200bb\u00adc\ufeffd\x00e\x1b[0m") == "abcde[0m"
    assert visible("one\u2028two\u2029three") == "one\ntwo\nthree"
    # A single joiner inside a word or an emoji is kept, as the gateway keeps it; a second
    # one in a row, or one after another invisible character, is not.
    assert visible("\u0646\u200c\u0647") == "\u0646\u200c\u0647"
    assert visible("a\u200d\u200db") == "a\u200db"
    assert visible("a\u200b\u200db") == "ab"
    assert visible("tab\tand\nline") == "tab\tand\nline"


def test_the_body_of_a_reply() -> None:
    body = with_quote("  Thanks!\n\n", "On ..., Alice wrote:", "Hello\n\nBye\n")
    assert body == "Thanks!\n\n> On ..., Alice wrote:\n> \n> Hello\n> \n> Bye"


def test_a_very_long_message_is_not_quoted_in_full() -> None:
    original = "\n".join(f"line {number}" for number in range(1000))
    body = with_quote("Thanks!", "On ..., Alice wrote:", original, limit=500)
    assert len(body) <= 500
    lines = body.split("\n")
    assert lines[:5] == ["Thanks!", "", "> On ..., Alice wrote:", "> ", "> line 0"]
    assert lines[-2:] == ["> ", f"> {CUT_SHORT}"]
    assert all(line.startswith("> line ") for line in lines[4:-2]), "no line is cut in half"
    # Without room for even one line, the note alone stands in for the quotation.
    tight = with_quote("Thanks!", "On ..., Alice wrote:", "x" * 400, limit=90)
    assert tight == f"Thanks!\n\n> On ..., Alice wrote:\n> \n> {CUT_SHORT}"
    # The reply itself is never shortened.
    assert with_quote("y" * 200, "On ..., Alice wrote:", "x", limit=100) == "y" * 200

"""Splitting a top-posted reply into new text, signature, and quotation."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from triage.mime import SplitText, split_quoted

QUOTE = "> Hi Robin,\n>\n> can you send me the figures?\n>\n> Kim"


@pytest.mark.parametrize(
    "introducer",
    [
        "On Mon, 2 Nov 2026 at 09:30, Kim Placeholder <kim@example.net> wrote:",
        "On 02/11/2026 09:30, Kim Placeholder wrote:",
        "On Mon, Nov 2, 2026 at 9:30 AM Kim Placeholder <kim@example.net>\nwrote:",
        "On Monday, 2 November 2026, Kim Placeholder\n<kim@example.net>\nwrote:",
        "Am 02.11.2026 um 09:30 schrieb Kim Placeholder:",
        "Am Mo., 2. Nov. 2026 um 09:30 Uhr schrieb Kim Placeholder <kim@example.net>:",
        "Le lun. 2 nov. 2026 à 09:30, Kim Placeholder <kim@example.net> a écrit :",
        "El lun, 2 nov 2026 a las 9:30, Kim Placeholder (<kim@example.net>) escribió:",
        "Il giorno lun 2 nov 2026 alle ore 09:30 Kim Placeholder ha scritto:",
        "Op ma 2 nov 2026 om 09:30 schreef Kim Placeholder <kim@example.net>:",
        "Kim Placeholder <kim@example.net> wrote:",
    ],
)
def test_reply_above_a_quotation(introducer: str) -> None:
    text = f"Sure, here they are.\n\nRobin\n\n{introducer}\n{QUOTE}\n"
    assert split_quoted(text) == SplitText(
        body="Sure, here they are.\n\nRobin", signature="", quoted=f"{introducer}\n{QUOTE}"
    )


def test_quotation_without_introducer() -> None:
    assert split_quoted(f"Done.\n\n{QUOTE}\n") == SplitText("Done.", "", QUOTE)


def test_unprefixed_quotation_after_introducer() -> None:
    text = (
        "Thanks!\n\nOn 2 Nov 2026, at 09:30, Kim Placeholder <kim@example.net> wrote:\n\n"
        "Hi Robin,\ncan you send me the figures?\n"
    )
    split = split_quoted(text)
    assert split.body == "Thanks!"
    assert split.quoted.startswith("On 2 Nov 2026")
    assert split.quoted.endswith("the figures?")


@pytest.mark.parametrize(
    "separator",
    [
        "-----Original Message-----",
        "----- Original Message -----",
        "-------- Original Message --------",
        "-----Ursprüngliche Nachricht-----",
        "________________________________\nFrom: Kim Placeholder <kim@example.net>\n"
        "Sent: Monday, November 2, 2026 9:30 AM\nTo: Robin Tester <robin@example.org>\n"
        "Subject: Figures",
        "From: Kim Placeholder <kim@example.net>\nDate: Monday, 2 November 2026 at 09:30\n"
        "To: Robin Tester <robin@example.org>\nSubject: Figures",
        "Von: Kim Placeholder <kim@example.net>\nGesendet: Montag, 2. November 2026 09:30\n"
        "An: Robin Tester <robin@example.org>\nBetreff: Zahlen",
    ],
)
def test_outlook_style_quotation(separator: str) -> None:
    text = f"Please see below.\n\n{separator}\n\nHi Robin,\ncan you send me the figures?\n"
    split = split_quoted(text)
    assert split.body == "Please see below."
    assert split.quoted.startswith(separator)
    assert split.quoted.endswith("the figures?")


def test_signature_is_separated() -> None:
    text = f"Sure.\n\n-- \nRobin Tester\nExample Org\n\nOn 2 Nov 2026, Kim wrote:\n{QUOTE}\n"
    split = split_quoted(text)
    assert split.body == "Sure."
    assert split.signature == "Robin Tester\nExample Org"
    assert split.quoted.startswith("On 2 Nov 2026")

    # The trailing space of the delimiter is often lost in transit.
    assert split_quoted("Sure.\n--\nRobin\n") == SplitText("Sure.", "Robin", "")
    # A long tail is not a signature, and a delimiter with nothing above it is not one either.
    long_tail = "Text\n-- \n" + "\n".join(f"line {i}" for i in range(40))
    assert split_quoted(long_tail).signature == ""
    assert split_quoted("-- \nonly a signature").body == "-- \nonly a signature"
    # Dashes inside the text are not a delimiter.
    assert split_quoted("a -- b\n---\nc").signature == ""


def test_interleaved_replies_are_left_whole() -> None:
    text = (
        "On 2 Nov 2026, Kim Placeholder wrote:\n"
        "> can you send me the figures?\n\nYes, attached.\n\n"
        "> And the report?\n\nTomorrow.\n"
    )
    assert split_quoted(text) == SplitText(text.strip("\n"), "", "")


def test_inline_quote_followed_by_trailing_quotation() -> None:
    text = (
        "You said:\n> the figures\nand I agree.\n\n"
        "On 2 Nov 2026, Kim Placeholder wrote:\n> can you send me the figures?\n"
    )
    split = split_quoted(text)
    assert split.body == "You said:\n> the figures\nand I agree."
    assert split.quoted.startswith("On 2 Nov 2026")


def test_messages_that_are_only_a_quotation_or_have_none() -> None:
    assert split_quoted(QUOTE) == SplitText(QUOTE, "", "")
    forwarded = "-----Original Message-----\nFrom: someone\n\ntext"
    assert split_quoted(forwarded).body == forwarded
    assert split_quoted("Just a message.\nNothing quoted.") == SplitText(
        "Just a message.\nNothing quoted.", "", ""
    )
    assert split_quoted("") == SplitText("", "", "")
    assert split_quoted("On the other hand, Kim wrote:") == SplitText(
        "On the other hand, Kim wrote:", "", ""
    )


def test_ordinary_sentences_are_not_mistaken_for_introducers() -> None:
    text = "On Monday I will be away.\nFrom: now on, please write to Kim.\nSent: nothing yet."
    assert split_quoted(text).quoted == ""
    text = "On reflection this is fine.\n\nOne thing though, as you wrote: it is late."
    assert split_quoted(text).quoted == ""


def test_crlf_line_endings() -> None:
    split = split_quoted("Sure.\r\n\r\nOn 2 Nov 2026, Kim wrote:\r\n> question\r\n")
    assert split == SplitText("Sure.", "", "On 2 Nov 2026, Kim wrote:\n> question")


def test_large_input_is_fast() -> None:
    text = "line\n> quoted\n" * 100_000 + "On " + "x" * 5000 + "\n" * 10
    assert split_quoted(text).quoted == ""
    assert split_quoted("> q\n" * 200_000).body.startswith("> q")
    assert split_quoted("On " + "a " * 100_000 + "wrote:\n> x").body


@settings(max_examples=300, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            [
                "", "text", "> quoted", ">> deeper", "-- ", "--", "On Monday, Kim wrote:",
                "-----Original Message-----", "From: a@example.org", "Sent: today",
                "To: b@example.org", "Subject: s", "wrote:", "____________",
            ]
        ),
        max_size=25,
    ).map("\n".join)
)  # fmt: skip
def test_nothing_is_ever_lost(text: str) -> None:
    """Whatever the split, the pieces put together contain every line of the original."""
    split = split_quoted(text)
    recovered = "\n".join(
        part for part in (split.body, split.signature, split.quoted) if part
    ).split("\n")
    delimiters = ("-- ", "--")
    original = [line for line in text.split("\n") if line.strip()]
    kept = [line for line in recovered if line.strip()]
    missing = list(original)
    for line in kept:
        if line in missing:
            missing.remove(line)
    assert all(line in delimiters for line in missing)
    assert len(missing) <= 1

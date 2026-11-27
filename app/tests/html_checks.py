"""An independent check that sanitised HTML cannot run code or fetch anything.

The check re-parses the output with a different parser than the sanitiser uses and inspects
every element and attribute, rather than trusting what the sanitiser believes it produced.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "a", "abbr", "acronym", "address", "b", "bdi", "bdo", "big", "blockquote", "br", "caption",
    "center", "cite", "code", "col", "colgroup", "dd", "del", "dfn", "div", "dl", "dt", "em",
    "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd", "li", "mark",
    "ol", "p", "pre", "q", "s", "samp", "small", "span", "strike", "strong", "sub", "sup",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "tt", "u", "ul", "var", "wbr",
}  # fmt: skip

# Attributes whose values are plain text or simple tokens and can never be a URL.
INERT_ATTRIBUTES = {
    "alt", "title", "class", "lang", "dir", "align", "valign", "width", "height", "border",
    "cellpadding", "cellspacing", "colspan", "rowspan", "span", "start", "type", "bgcolor",
    "color", "face", "size", "rel", "target", "data-triage-blocked-src",
}  # fmt: skip

SAFE_HREF = re.compile(r"(https?://|mailto:)\S+\Z", re.IGNORECASE)
SAFE_SRC = re.compile(r"data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}\Z")
SAFE_STYLE = re.compile(r"[A-Za-z0-9 #%.,()+\-!/:;]*\Z")
STYLE_FUNCTION = re.compile(r"([A-Za-z-]*)\s*\(")
TOKEN = re.compile(r"[A-Za-z0-9#%+., _-]*\Z")


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"tag <{tag}>")
        for name, value in attrs:
            value = value or ""
            self.attributes.append((tag, name, value))
            if name == "href" and tag == "a":
                if not SAFE_HREF.match(value):
                    self.problems.append(f"href {value!r}")
            elif name == "src" and tag == "img":
                if not SAFE_SRC.match(value):
                    self.problems.append(f"src {value!r}")
            elif name == "style":
                functions = {f.lower() for f in STYLE_FUNCTION.findall(value)}
                if not SAFE_STYLE.match(value) or functions - {"rgb", "rgba", "hsl", "hsla"}:
                    self.problems.append(f"style {value!r}")
            elif name in INERT_ATTRIBUTES:
                if name not in ("alt", "title", "class", "lang") and not TOKEN.match(value):
                    self.problems.append(f"{name} {value!r}")
            else:
                self.problems.append(f"attribute {name} on <{tag}>")
            if tag == "a" and name == "target" and value != "_blank":
                self.problems.append(f"target {value!r}")

    def handle_comment(self, data: str) -> None:
        self.problems.append("comment")

    def handle_decl(self, decl: str) -> None:
        self.problems.append("declaration")

    def handle_pi(self, data: str) -> None:
        self.problems.append("processing instruction")

    def unknown_decl(self, data: str) -> None:
        self.problems.append("unknown declaration")


def inspect(html: str) -> _Checker:
    checker = _Checker()
    checker.feed(html)
    checker.close()
    return checker


def problems(html: str) -> list[str]:
    """Everything in the HTML that could run code or fetch a resource. Empty means safe."""
    return inspect(html).problems

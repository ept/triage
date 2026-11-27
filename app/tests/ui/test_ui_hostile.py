"""The web interface against a backend whose every text field carries hostile markup.

Whatever the backend sends is shown as text. Nothing in it may run, load anything, or become a
link, and even an email whose HTML was not sanitised at all stays confined to its frame.
"""

from __future__ import annotations

from conftest import Session
from playwright.sync_api import Page, expect
from stub_server import HOSTILE

# Elements that the interface never creates itself, so any of them came from injected markup.
# (The only frame is the email frame, the only script is the app's module, and the only forms
# are the two composers.)
INJECTED = (
    "img, svg, object, embed, form:not(#composer):not(#coding-form), video, audio, link:not([rel=icon]):not([rel=stylesheet]), "
    "iframe:not(.message-frame), script:not([src='js/main.js']), a[href^='javascript'], "
    "a:not(.conversation), [onerror], [onload], [onclick], base, meta[http-equiv]"
)


def assert_inert(session: Session, page: Page) -> None:
    assert not session.pwned()
    assert page.locator(INJECTED).count() == 0
    assert session.unexpected_requests() == []


def test_hostile_text_is_shown_as_text_everywhere(hostile_ui: Session) -> None:
    ui = hostile_ui
    page = ui.open("#chat/1")

    # --- Chat: every kind of item.
    items = page.locator("#chat-items")
    expect(items.locator(".item-user .bubble")).to_contain_text(
        '<img src=x onerror="window.__pwned=1">'
    )
    reply = items.locator(".item-assistant .markdown").first
    expect(reply).to_contain_text("<script>window.__pwned=1</script>")
    # A Markdown image stays as its source text, and a link is text with its address visible.
    expect(reply).to_contain_text("![pic](https://evil.example/pixel.png)")
    expect(reply.locator(".link-text").first).to_have_text("click me (javascript:window.__pwned=1)")
    expect(reply).to_contain_text("https://evil.example/bare-url")
    expect(items.locator(".tool-line").first).to_contain_text("<iframe")
    expect(items.locator(".change-request .request-text")).to_contain_text("<script>")
    expect(items.locator(".proposal .diff")).to_contain_text("<svg onload=")
    expect(items.locator(".notice-line").last).to_contain_text("<img src=x")
    expect(page.locator("#chat-heading")).to_contain_text("<script>")
    expect(page.locator("#conversation-list a")).to_contain_text("<img src=x")
    expect(page.locator("#notices")).to_contain_text("<script>")
    expect(page.locator("#status-body")).to_contain_text("stub-model <img src=x")
    assert_inert(ui, page)

    # --- Message cards and the viewer.
    items.locator(".tool-cards summary").click()
    expect(items.locator(".message-card").first).to_contain_text("<img src=x")
    items.locator(".message-card").first.click()
    viewer = page.locator("#viewer")
    expect(viewer.locator(".viewer-subject")).to_contain_text("Budget figures <img src=x")
    expect(viewer.locator(".meta")).to_contain_text("<script>")
    expect(viewer.locator(".analysis")).to_contain_text("<img src=x")
    expect(viewer.locator(".attachments")).to_contain_text("figures.pdf <img src=x")
    expect(viewer.locator(".message-body")).to_contain_text(
        "[click me](javascript:window.__pwned=1)"
    )
    assert_inert(ui, page)

    # --- A streamed reply.
    box = page.locator("#composer-text")
    box.fill("hello " + HOSTILE)
    box.press("Enter")
    final = page.locator(".item-assistant:not(.streaming) .markdown").last
    expect(final).to_contain_text("You have two messages to answer")
    expect(final).to_contain_text("<script>window.__pwned=1</script>")
    box.fill("please fail")
    box.press("Enter")
    expect(page.locator(".item-error")).to_contain_text("<img src=x")
    assert_inert(ui, page)

    # --- The other views.
    page.locator("#nav [data-view=activity]").click()
    expect(page.locator("#activity-list .entry").first).to_contain_text("<script>")
    expect(page.locator("#activity-list .error-text").first).to_contain_text("<img src=x")
    assert_inert(ui, page)

    page.locator("#nav [data-view=policy]").click()
    expect(page.locator("#policy-proposals .diff")).to_contain_text("<img src=x")
    assert "<script>" in page.locator("#policy-text").input_value()
    page.locator("#policy-history button").first.click()
    expect(page.locator("#policy-version-text")).to_contain_text("<img src=x")
    assert_inert(ui, page)

    page.locator("#nav [data-view=coding]").click()
    expect(page.locator("#coding-requests .request-text").first).to_contain_text("<script>")
    expect(page.locator("#coding-output .coding-text").first).to_contain_text("<img src=x")
    expect(page.locator("#coding-output")).to_contain_text(
        "[click me](javascript:window.__pwned=1)"
    )
    assert_inert(ui, page)

    # Nothing was even attempted so far: no violations of the policy, and no errors.
    assert ui.policy_violations() == []
    assert ui.console_errors == []

    # --- A failing request, whose error message is hostile too.
    ui.backend.fail_next("POST", "/api/activity/7/undo", 502, "imap_error", "No." + HOSTILE)
    page.locator("#nav [data-view=activity]").click()
    page.locator("#activity-list").get_by_role("button").first.click()
    expect(page.locator("#error-bar")).to_contain_text("No. <img src=x")
    assert_inert(ui, page)

    assert ui.policy_violations() == []
    # The browser logs the failed request itself, and nothing else.
    assert len(ui.console_errors) == 1
    assert "502" in ui.console_errors[0]


def test_an_unsanitised_email_is_confined_to_its_frame(hostile_ui: Session) -> None:
    """Defence in depth: the frame must hold even if the backend's sanitiser failed entirely."""
    ui = hostile_ui
    page = ui.open("#chat/1")
    page.locator(".item-assistant button.handle").first.click()
    page.locator("#viewer").get_by_role("button", name="Formatted").click()
    frame = page.frame_locator("iframe.message-frame")
    expect(frame.locator("body")).to_contain_text("Unsanitised on purpose.")
    # Give scripts, image loads and the meta refresh every chance to do something.
    page.wait_for_timeout(1500)

    assert not ui.pwned(), "a script from the email ran"
    assert ui.unexpected_requests() == [], "the email made the browser request something"
    assert page.url.endswith("#chat/1"), "the email navigated the page"
    expect(frame.locator("body")).to_contain_text("Unsanitised on purpose.")
    assert page.locator("iframe.message-frame").get_attribute("sandbox") == ""

    # Clicking links inside the frame does nothing: neither the page nor the frame goes anywhere.
    for link in (frame.locator("a").first, frame.locator("#stay")):
        link.click(force=True, no_wait_after=True)
    page.wait_for_timeout(500)
    expect(frame.locator("body")).to_contain_text("Unsanitised on purpose.")
    assert page.url.endswith("#chat/1")
    assert ui.unexpected_requests() == []

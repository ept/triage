"""The web interface in a real browser, against the stub backend."""

from __future__ import annotations

import re

from conftest import Session
from playwright.sync_api import expect
from stub_server import WEB_DIR


def test_static_files_have_nothing_inline() -> None:
    """The Content-Security-Policy forbids inline scripts; do not rely on the browser to say so."""
    html = (WEB_DIR / "index.html").read_text()
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert scripts == ['<script type="module" src="js/main.js">']
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
    assert "javascript:" not in html
    assert not re.search(r"""(?:src|href)=["'](?:https?:)?//""", html), "external resource"
    for script in (WEB_DIR / "js").glob("*.js"):
        source = script.read_text()
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
        ):
            assert forbidden not in source, f"{script.name} uses {forbidden}"
        assert not re.search(r"""['"`]https?://""", source), f"{script.name} has an absolute URL"
    css = (WEB_DIR / "app.css").read_text()
    assert "url(" not in css
    assert "@import" not in css


def test_page_loads_cleanly(ui: Session) -> None:
    page = ui.open()
    expect(page.locator("#nav .nav-item[aria-current=page]")).to_have_text("Chat")
    expect(page.locator("#status-body")).to_contain_text("robin@example.org")
    expect(page.locator("#status-body")).to_contain_text("Inbox: 240 messages, 12 unread")
    expect(page.locator("#status-body")).to_contain_text("Local model ready: stub-model")
    expect(page.locator("#status-body")).to_contain_text("the IMAP server did not answer in time")
    expect(page.locator("#conversation-list a")).to_have_text(["This week's deadlines"])
    expect(page.locator(".hint", has_text="model running on this computer")).to_be_visible()
    # Things waiting for the user are counted on the navigation.
    expect(page.locator("#badge-coding")).to_have_text("1")
    expect(page.locator("#badge-policy")).to_have_text("1")
    assert ("GET", "/api/status", None) in ui.backend.requests
    ui.assert_clean()


def test_conversation_shows_every_kind_of_item(ui: Session) -> None:
    page = ui.open("#chat/1")
    items = page.locator("#chat-items")
    expect(page.locator("#chat-heading")).to_have_text("This week's deadlines")
    expect(items.locator(".item-user .bubble")).to_have_text(
        "What do I need to reply to this week?"
    )

    # Tool calls are one-line notes; failed ones are marked; results stay folded away.
    expect(items.locator(".tool-line").first).to_contain_text("Searched mail that needs a reply")
    expect(items.locator(".tool-line.failed")).to_contain_text("(failed)")
    expect(items.locator(".tool-cards .message-card").first).to_be_hidden()
    items.locator(".tool-cards summary").click()
    cards = items.locator(".tool-cards .message-card")
    expect(cards).to_have_count(3)
    expect(cards.first).to_contain_text("Alex Sample")
    expect(cards.first).to_contain_text("Budget figures")
    expect(cards.first.locator(".marker.unread")).to_be_visible()
    expect(cards.first.locator(".marker.attachment")).to_be_visible()

    # The assistant's reply is rendered as Markdown.
    reply = items.locator(".item-assistant .markdown").first
    expect(reply.locator("strong")).to_have_text("two")
    expect(reply.locator("em")).to_have_text("budget")
    expect(reply.locator("code")).to_have_text("Archive")
    expect(reply.locator("ol > li")).to_have_count(2)
    expect(reply.locator("button.handle")).to_have_text(["[m1]", "[m3]", "[m2]"])
    expect(reply).to_contain_text("Unknown handle: [m99].")

    # A change request says where it must be approved, and shows its exact text.
    request = items.locator(".change-request")
    expect(request).to_contain_text("Bug report")
    expect(request.locator(".state")).to_have_text("waiting for approval")
    expect(request.locator(".request-text")).to_have_text(
        "Parsing fails for a message with an empty Date header."
    )
    expect(request).to_contain_text("Email Triage window outside this page")
    expect(request).to_contain_text("remote model only once you approve")

    expect(items.locator(".proposal .diff-add")).to_have_text("+Always flag mail from Kim.")
    expect(items.locator(".proposal .diff-remove")).to_have_text("-Flag nothing.")
    expect(items.locator(".notice-line").last).to_have_text(
        "The policy was applied to 4 new messages."
    )
    ui.assert_clean()


def test_sending_a_message_streams_the_reply(ui: Session) -> None:
    page = ui.open()
    box = page.locator("#composer-text")
    box.fill("What is new?")
    box.press("Enter")

    # While the reply is being written it is shown as it arrives ...
    streaming = page.locator(".item.streaming")
    expect(streaming).to_be_visible()
    expect(page.locator("#composer-send")).to_be_disabled()
    expect(streaming).to_contain_text("You have")
    # ... and is then replaced by the finished item.
    final = page.locator(".item-assistant:not(.streaming) .markdown")
    expect(final).to_have_text("You have two messages to answer: [m1] and [m3].")
    expect(streaming).to_have_count(0)
    expect(page.locator(".item-user .bubble")).to_have_text("What is new?")
    expect(page.locator(".item-user.pending")).to_have_count(0)
    expect(page.locator(".tool-line")).to_contain_text("Searched mail")
    expect(page.locator("#composer-send")).to_be_enabled()
    expect(box).to_have_value("")

    # A conversation was created for the first message, and has its own address now.
    assert ui.backend.seen("POST", "/api/conversations") == [{}]
    assert ui.backend.seen("POST", "/api/conversations/2/messages") == [{"text": "What is new?"}]
    assert page.url.endswith("#chat/2")
    expect(page.locator("#conversation-list a")).to_have_count(2)

    # The handles in the reply open the messages found by the tool call before it.
    final.locator("button.handle", has_text="[m3]").click()
    expect(page.locator("#viewer .viewer-subject")).to_have_text("Lunch on Thursday?")

    # A second message goes to the same conversation.
    box.fill("And again")
    page.locator("#composer-send").click()
    expect(page.locator(".item-assistant:not(.streaming)")).to_have_count(2)
    assert len(ui.backend.seen("POST", "/api/conversations/2/messages")) == 2
    assert len(ui.backend.seen("POST", "/api/conversations")) == 1
    ui.assert_clean()


def test_shift_enter_starts_a_new_line(ui: Session) -> None:
    page = ui.open()
    box = page.locator("#composer-text")
    box.fill("first")
    box.press("Shift+Enter")
    box.press_sequentially("second")
    expect(box).to_have_value("first\nsecond")
    assert ui.backend.seen("POST", "/api/conversations") == []
    # An empty message is not sent.
    box.fill("   ")
    box.press("Enter")
    assert ui.backend.seen("POST", "/api/conversations") == []


def test_errors_during_a_reply_are_shown(ui: Session) -> None:
    page = ui.open("#chat/1")
    box = page.locator("#composer-text")
    box.fill("please fail")
    box.press("Enter")
    expect(page.locator(".item-error")).to_have_text("The local model is not available.")
    expect(page.locator("#composer-send")).to_be_enabled()

    # A request that fails outright is reported in the conversation too.
    ui.backend.fail_next("POST", "/api/conversations/1/messages", 503, "unavailable", "Try later.")
    box.fill("hello")
    box.press("Enter")
    expect(page.locator(".item-error").last).to_have_text("Try later.")
    expect(page.locator(".item-user.unconfirmed")).to_have_count(1)


def test_message_viewer(ui: Session) -> None:
    page = ui.open("#chat/1")
    page.locator(".item-assistant button.handle", has_text="[m1]").click()
    viewer = page.locator("#viewer")
    expect(viewer.locator(".viewer-subject")).to_have_text("Budget figures")
    expect(viewer.locator(".meta")).to_contain_text("Alex Sample <alex@example.com>")
    expect(viewer.locator(".meta")).to_contain_text("Robin Tester <robin@example.org>")
    expect(viewer.locator(".meta")).to_contain_text("lee@example.com")
    expect(viewer.locator(".analysis")).to_contain_text("asks for a reply")
    expect(viewer.locator(".analysis")).to_contain_text("needs a reply · high urgency")
    expect(viewer.locator(".attachments")).to_contain_text("figures.pdf")

    # Plain text is the default; signature and quoted text are folded away.
    expect(viewer.locator("iframe")).to_have_count(0)
    expect(viewer.locator(".message-body > .message-text")).to_contain_text(
        "please find the figures"
    )
    quoted = viewer.locator("details", has_text="Show quoted text")
    expect(quoted.locator(".message-text")).to_be_hidden()
    quoted.locator("summary").click()
    expect(quoted.locator(".message-text")).to_contain_text("Could you send the figures?")
    expect(viewer.locator("details", has_text="Show signature")).to_have_count(1)

    # The formatted view is a frame that can do nothing.
    viewer.get_by_role("button", name="Formatted").click()
    frame = viewer.locator("iframe.message-frame")
    expect(frame).to_have_count(1)
    assert frame.get_attribute("sandbox") == ""
    assert frame.get_attribute("src") is None
    assert not any(a.startswith("allow") for a in frame.evaluate("e => e.getAttributeNames()"))
    expect(viewer.locator(".blocked-note")).to_have_text("2 remote images were blocked.")
    expect(page.frame_locator("iframe.message-frame").locator("body")).to_contain_text(
        "please find the figures attached"
    )
    assert ui.backend.seen("GET", "/api/messages/key-m1/html") == [None]

    viewer.get_by_role("button", name="Plain text").click()
    expect(viewer.locator("iframe")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(viewer).to_be_hidden()

    # A message without an HTML part offers no formatted view.
    page.locator(".tool-cards summary").click()
    page.locator(".message-card", has_text="Lunch on Thursday?").click()
    expect(viewer.locator(".viewer-subject")).to_have_text("Lunch on Thursday?")
    expect(viewer.get_by_role("button", name="Formatted")).to_have_count(0)
    viewer.get_by_role("button", name="Close message").click()
    expect(viewer).to_be_hidden()
    ui.assert_clean()


def test_policy_proposal_can_be_accepted_in_the_chat(ui: Session) -> None:
    page = ui.open("#chat/1")
    proposal = page.locator(".item .proposal")
    proposal.get_by_role("button", name="Accept").click()
    expect(proposal.locator(".state")).to_have_text("accepted")
    expect(proposal.get_by_role("button")).to_have_count(0)
    assert ui.backend.seen("POST", "/api/policy/proposals/p1") == [{"accept": True}]
    assert ui.backend.proposal["state"] == "accepted"
    ui.assert_clean()


def test_policy_view(ui: Session) -> None:
    page = ui.open("#policy")
    editor = page.locator("#policy-text")
    save = page.locator("#policy-save")
    expect(editor).to_have_value("Archive newsletters after a week.\nFlag nothing.\n")
    expect(save).to_be_disabled()
    expect(page.locator("#policy-updated")).to_contain_text("Last changed")

    # Reject the pending proposal.
    proposal = page.locator("#policy-proposals .proposal")
    expect(proposal.locator(".diff-add")).to_have_text("+Always flag mail from Kim.")
    proposal.get_by_role("button", name="Reject").click()
    expect(proposal.locator(".state")).to_have_text("rejected")
    assert ui.backend.seen("POST", "/api/policy/proposals/p1") == [{"accept": False}]

    # Edit and save.
    editor.fill("Never archive anything.\n")
    expect(save).to_be_enabled()
    save.click()
    expect(save).to_be_disabled()
    assert ui.backend.seen("PUT", "/api/policy") == [{"text": "Never archive anything.\n"}]

    # Look at an earlier version and restore it into the editor (which does not save it).
    expect(page.locator("#policy-history button")).to_have_count(2)
    page.locator("#policy-history button", has_text="First version").click()
    expect(page.locator("#policy-version-text")).to_have_text(
        "Archive newsletters after a month.\n"
    )
    page.locator("#policy-restore").click()
    expect(editor).to_have_value("Archive newsletters after a month.\n")
    expect(save).to_be_enabled()
    assert len(ui.backend.seen("PUT", "/api/policy")) == 1
    ui.assert_clean()


def test_accepting_a_proposal_in_the_policy_view_updates_the_editor(ui: Session) -> None:
    page = ui.open("#policy")
    page.locator("#policy-proposals").get_by_role("button", name="Accept").click()
    expect(page.locator("#policy-text")).to_have_value(
        "Archive newsletters after a week.\nAlways flag mail from Kim.\n"
    )
    expect(page.locator("#policy-proposals .proposal")).to_have_count(0)
    ui.assert_clean()


def test_a_refused_request_is_reported(ui: Session) -> None:
    page = ui.open("#policy")
    ui.backend.fail_next(
        "PUT", "/api/policy", 403, "forbidden", "The policy cannot be changed now."
    )
    page.locator("#policy-text").fill("x")
    page.locator("#policy-save").click()
    expect(page.locator("#error-bar")).to_contain_text("The policy cannot be changed now.")
    expect(page.locator("#policy-save")).to_be_enabled()
    page.locator("#error-dismiss").click()
    expect(page.locator("#error-bar")).to_be_hidden()


def test_activity_and_undo(ui: Session) -> None:
    page = ui.open("#activity")
    entries = page.locator("#activity-list .entry")
    expect(entries).to_have_count(4)
    # Newest first.
    expect(entries.first).to_contain_text("Moved 1 message from INBOX to Trash")
    expect(entries.first).to_contain_text("Failed: the IMAP server refused the command")
    expect(entries.first.get_by_role("button")).to_have_count(0)

    moved = page.locator(".entry", has_text="Moved 3 messages from INBOX to Archive")
    expect(page.locator(".entry", has_text="Marked 2 messages as read")).to_contain_text("undone")
    expect(
        page.locator("#activity-list").get_by_role("button", name=re.compile("Undo"))
    ).to_have_count(1)
    moved.get_by_role("button", name=re.compile("Undo")).click()
    expect(moved).to_contain_text("undone")
    expect(moved.get_by_role("button")).to_have_count(0)
    assert ui.backend.seen("POST", "/api/activity/7/undo") == [{}]
    ui.assert_clean()


def test_coding_agent_view(ui: Session) -> None:
    page = ui.open("#coding")
    requests = page.locator("#coding-requests .change-request")
    expect(requests).to_have_count(2)
    expect(requests.first).to_contain_text("Bug report")
    expect(requests.first.locator(".state")).to_have_text("waiting for approval")
    expect(requests.nth(1).locator(".state")).to_have_text("approved")
    expect(requests.nth(1).locator(".request-text")).to_have_text("Please add a dark mode.")

    # The agent's output is plain text: Markdown is not interpreted.
    output = page.locator("#coding-output")
    expect(output.locator(".coding-event")).to_have_count(4)
    expect(output.locator(".coding-text").nth(1)).to_have_text(
        "I have added a **dark** colour scheme."
    )
    expect(output.locator("strong")).to_have_count(0)
    expect(output.locator(".coding-kind-question")).to_contain_text("The coding agent asks:")
    expect(output.locator(".coding-kind-status .coding-text")).to_contain_text("Edit: /workspace")

    # The last thing the agent said is a question, so what the user writes is a reply.
    expect(page.locator("#coding-label")).to_have_text("Reply to the coding agent")
    expect(page.locator("#coding-form")).to_contain_text("leaves this computer")
    page.locator("#coding-text").fill("Yes, follow the system setting.")
    page.locator("#coding-send").click()
    expect(requests).to_have_count(3)
    expect(requests.first.locator(".request-text")).to_have_text("Yes, follow the system setting.")
    expect(requests.first).to_contain_text("Reply to the coding agent")
    assert ui.backend.seen("POST", "/api/coding/requests") == [
        {"kind": "reply", "text": "Yes, follow the system setting."}
    ]
    expect(page.locator("#coding-text")).to_have_value("")

    # The view keeps following the agent, and shows nothing twice.
    ui.backend.coding_events.append(
        {"seq": 5, "time": "2026-11-25T10:00:00+00:00", "request_id": "cr-3", "kind": "text",
         "text": "Understood."}
    )  # fmt: skip
    expect(output.locator(".coding-event")).to_have_count(5)
    expect(page.locator("#coding-label")).to_have_text("Ask for a change to the app")
    ui.assert_clean()


def test_a_new_request_to_the_coding_agent_is_a_feature_request(ui: Session) -> None:
    ui.backend.coding_events.clear()
    ui.backend.coding_events.append(
        {
            "seq": 1,
            "time": "2026-11-25T10:00:00+00:00",
            "request_id": None,
            "kind": "done",
            "text": "",
        }
    )
    page = ui.open("#coding")
    expect(page.locator("#coding-label")).to_have_text("Ask for a change to the app")
    page.locator("#coding-text").fill("Add a search box.")
    page.locator("#coding-send").click()
    expect(page.locator("#coding-requests .change-request")).to_have_count(3)
    assert ui.backend.seen("POST", "/api/coding/requests") == [
        {"kind": "feature", "text": "Add a search box."}
    ]


def test_notices_and_sync(ui: Session) -> None:
    page = ui.open()
    notice = page.locator("#notices .notice")
    expect(notice).to_contain_text("23 messages were moved to the trash within an hour.")
    notice.get_by_role("button", name="Dismiss").click()
    expect(notice).to_have_count(0)

    expect(page.locator("#sync-progress")).to_have_count(0)
    page.locator("#sync-now").click()
    expect(page.locator("#sync-progress")).to_be_visible()
    expect(page.locator("#status-body")).to_contain_text("Fetching mail: 60 of 240")
    expect(page.locator("#sync-now")).to_be_disabled()
    assert ui.backend.seen("POST", "/api/sync") == [{}]
    # The dismissed notice stays dismissed when the status is fetched again.
    expect(notice).to_have_count(0)
    ui.assert_clean()


def test_navigation_keeps_the_place(ui: Session) -> None:
    page = ui.open("#chat/1")
    expect(page.locator(".item-user")).to_have_count(1)
    page.locator("#nav [data-view=activity]").click()
    expect(page.locator("#view-activity")).to_be_visible()
    expect(page.locator("#view-chat")).to_be_hidden()
    assert page.url.endswith("#activity")
    page.locator("#nav [data-view=chat]").click()
    assert page.url.endswith("#chat/1")
    expect(page.locator(".item-user")).to_have_count(1)
    page.locator("#new-conversation").click()
    expect(page.locator("#chat-empty")).to_be_visible()
    expect(page.locator(".item-user")).to_have_count(0)
    page.go_back()
    expect(page.locator(".item-user")).to_have_count(1)
    ui.assert_clean()


def test_automatic_policy_setting(ui: Session) -> None:
    page = ui.open("#policy")
    box = page.locator("#policy-automatic")
    expect(box).not_to_be_checked()
    box.check()
    expect(box).to_be_checked()
    expect(box).to_be_enabled()
    assert ui.backend.seen("PUT", "/api/settings") == [{"apply_policy_automatically": True}]
    assert ui.backend.apply_policy_automatically is True
    # The setting is read back from the backend when the view is opened again.
    page.reload()
    expect(page.locator("#policy-automatic")).to_be_checked()
    ui.assert_clean()

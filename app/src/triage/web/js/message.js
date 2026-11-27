// Message cards and the message viewer.

import { api } from "./api.js";
import { byId, clear, el } from "./dom.js";
import { formatAddresses, formatSize, formatTime, shortSender } from "./format.js";
import { showError } from "./shared.js";

// Prepended to an email's HTML before it is put into its frame. The HTML has been sanitised by
// the backend, and the frame cannot run scripts; this policy is a third line of defence, which
// stops the frame from loading anything at all except images that are embedded in the message.
// Links cannot be clicked: following one would only replace the message with a page that the
// frame is not allowed to show. (Opening links is left to the wrapper app.)
const FRAME_POLICY =
  '<!doctype html><meta charset="utf-8">' +
  '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; ' +
  "img-src data:; style-src 'unsafe-inline'\">" +
  '<meta name="referrer" content="no-referrer">' +
  "<style>a { pointer-events: none !important; }</style>";

/** A compact summary of a message; pressing it opens the viewer. */
export function messageCard(card) {
  const markers = el("span", { class: "markers" });
  if (card.unread) markers.append(el("span", { class: "marker unread", title: "Unread", text: "●" }));
  if (card.flagged) markers.append(el("span", { class: "marker flagged", title: "Flagged", text: "⚑" }));
  if (card.has_attachments) {
    markers.append(el("span", { class: "marker attachment", title: "Has attachments", text: "attachment" }));
  }
  return el(
    "button",
    {
      type: "button",
      class: `message-card${card.unread ? " is-unread" : ""}`,
      dataset: { key: card.key, handle: card.handle || "" },
      on: { click: () => openMessage(card.key) },
    },
    el("span", { class: "card-line" },
      card.handle ? el("span", { class: "card-handle", text: card.handle }) : null,
      el("span", { class: "card-sender", text: shortSender(card.from) }),
      markers,
      el("span", { class: "card-date", text: formatTime(card.date) })),
    el("span", { class: "card-subject", text: card.subject || "(no subject)" }),
    el("span", { class: "card-preview", text: card.preview || "" }),
    el("span", { class: "card-folder", text: [card.account, card.folder].filter(Boolean).join(" · ") }),
  );
}

export function closeViewer() {
  const viewer = byId("viewer");
  viewer.hidden = true;
  clear(viewer);
  document.body.classList.remove("viewer-open");
}

function metaRow(label, value) {
  if (!value) return null;
  return el("div", { class: "meta-row" },
    el("span", { class: "meta-label", text: label }),
    el("span", { class: "meta-value", text: value }));
}

function collapsed(label, text) {
  if (!text || !text.trim()) return null;
  return el("details", { class: "collapsed" },
    el("summary", { text: label }),
    el("div", { class: "message-text", text }));
}

function textView(message) {
  const text = message.text || {};
  return el(
    "div",
    { class: "message-body", dataset: { view: "text" } },
    el("div", { class: "message-text", text: text.body || "(This message has no text.)" }),
    collapsed("Show signature", text.signature),
    collapsed("Show quoted text", text.quoted),
  );
}

/**
 * The HTML view. The message's HTML is only ever displayed inside a frame with an empty
 * `sandbox` attribute: no scripts, no access to this page, no pop-ups, no navigation of this
 * page. It is fetched by this page and handed to the frame as `srcdoc`, because the policy that
 * the gateway attaches to every response forbids framing a response directly.
 */
function htmlView(message) {
  const container = el("div", { class: "message-body", dataset: { view: "html" } });
  if (message.blocked_remote_content > 0) {
    const count = message.blocked_remote_content;
    container.append(el("p", {
      class: "blocked-note",
      text: `${count} remote image${count === 1 ? " was" : "s were"} blocked.`,
    }));
  }
  const frame = el("iframe", {
    class: "message-frame",
    sandbox: true,
    title: "Message content",
    referrerpolicy: "no-referrer",
  });
  container.append(frame);
  api.messageHtml(message.key).then(
    (html) => {
      frame.srcdoc = FRAME_POLICY + html;
    },
    (error) => {
      frame.replaceWith(el("p", { class: "error-text", text: error.message }));
    },
  );
  return container;
}

function analysisView(analysis) {
  if (!analysis) return null;
  const facts = [];
  if (analysis.category) facts.push(String(analysis.category));
  if (analysis.needs_reply) facts.push("needs a reply");
  if (analysis.urgency && analysis.urgency !== "normal") facts.push(`${analysis.urgency} urgency`);
  if (analysis.deadline) facts.push(`deadline ${analysis.deadline}`);
  return el("div", { class: "analysis" },
    el("div", { class: "analysis-title", text: "Summary by the local model" }),
    analysis.summary ? el("p", { text: String(analysis.summary) }) : null,
    facts.length ? el("p", { class: "hint", text: facts.join(" · ") }) : null);
}

function attachmentsView(attachments) {
  if (!attachments || attachments.length === 0) return null;
  const list = el("ul", { class: "plain-list attachments" });
  for (const attachment of attachments) {
    list.append(el("li", {},
      el("span", { class: "attachment-name", text: attachment.filename || "(unnamed)" }),
      el("span", { class: "hint", text: ` ${attachment.content_type || ""} ${formatSize(attachment.size)}` })));
  }
  return list;
}

function render(message) {
  const viewer = clear(byId("viewer"));
  const body = el("div", { class: "viewer-body" });
  const show = (view) => {
    body.replaceChildren(view === "html" ? htmlView(message) : textView(message));
    for (const button of toggle.querySelectorAll("button")) {
      button.setAttribute("aria-pressed", String(button.dataset.view === view));
    }
  };
  const toggle = el("div", { class: "view-toggle", role: "group", "aria-label": "View" });
  if (message.has_html) {
    toggle.append(
      el("button", { type: "button", class: "small", dataset: { view: "text" }, text: "Plain text",
        on: { click: () => show("text") } }),
      el("button", { type: "button", class: "small", dataset: { view: "html" }, text: "Formatted",
        on: { click: () => show("html") } }),
    );
  }
  viewer.append(
    el("div", { class: "viewer-head" },
      el("h2", { class: "viewer-subject", text: message.subject || "(no subject)" }),
      el("button", { type: "button", class: "small", id: "viewer-close", text: "Close",
        "aria-label": "Close message", on: { click: closeViewer } })),
    el("div", { class: "meta" },
      metaRow("From", formatAddresses(message.from)),
      metaRow("To", formatAddresses(message.to)),
      metaRow("Cc", formatAddresses(message.cc)),
      metaRow("Date", formatTime(message.date)),
      metaRow("Folder", [message.account, message.folder].filter(Boolean).join(" · "))),
    analysisView(message.analysis),
    attachmentsView(message.attachments),
    toggle,
    body,
  );
  // Plain text is the default; the formatted view is the user's choice.
  show("text");
}

/** Opens the viewer on a message. */
export async function openMessage(key) {
  const viewer = byId("viewer");
  viewer.hidden = false;
  document.body.classList.add("viewer-open");
  clear(viewer).append(el("p", { class: "hint", text: "Loading…" }));
  try {
    const message = await api.message(key);
    render(message);
    byId("viewer-close").focus();
  } catch (error) {
    closeViewer();
    showError(error);
  }
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !byId("viewer").hidden) closeViewer();
});

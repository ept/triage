// The chat view: a conversation with the model that runs on this computer.

import { api } from "./api.js";
import { byId, clear, el } from "./dom.js";
import { formatTime } from "./format.js";
import { renderMarkdown } from "./markdown.js";
import { messageCard, openMessage } from "./message.js";
import { attempt, changeRequestView, proposalView, showError } from "./shared.js";

let conversationId = null;
let busy = false;
// Message handle ("m3") -> card, for every message mentioned in this conversation.
const handles = new Map();
// Called when the conversation changes, so that the address bar and the sidebar can follow.
let onConversationChanged = () => {};

export function setConversationListener(listener) {
  onConversationChanged = listener;
}

export function currentConversation() {
  return conversationId;
}

const items = () => byId("chat-items");

function nearBottom() {
  const node = items();
  return node.scrollHeight - node.scrollTop - node.clientHeight < 120;
}

function scrollToBottom() {
  const node = items();
  node.scrollTop = node.scrollHeight;
}

function markdownOptions() {
  return { handles, onHandle: (card) => openMessage(card.key) };
}

function cardList(cards) {
  const list = el("div", { class: "card-list" });
  for (const card of cards) list.append(messageCard(card));
  return list;
}

/** Builds the element for one item of the conversation. */
function renderItem(item) {
  for (const card of item.messages || []) {
    if (card.handle) handles.set(card.handle, card);
  }
  const cards = item.messages || [];
  const node = el("div", {
    class: `item item-${["user", "assistant", "tool", "notice"].includes(item.role) ? item.role : "notice"}`,
    dataset: { itemId: item.id ?? "" },
  });

  if (item.role === "user") {
    node.append(el("div", { class: "bubble", text: item.text || "" }));
  } else if (item.role === "assistant") {
    const bubble = el("div", { class: "bubble markdown" });
    bubble.append(renderMarkdown(item.text || "", markdownOptions()));
    node.append(bubble);
    if (cards.length) node.append(cardList(cards));
  } else if (item.role === "tool") {
    const tool = item.tool || {};
    const line = el("div", { class: `tool-line${tool.ok === false ? " failed" : ""}` },
      el("span", { class: "tool-mark", "aria-hidden": "true", text: tool.ok === false ? "✕" : "✓" }),
      el("span", { class: "tool-summary", text: tool.summary || tool.name || item.text || "" }),
      tool.ok === false ? el("span", { class: "tool-failed", text: " (failed)" }) : null);
    node.append(line);
    if (cards.length) {
      // Search results and the like stay out of the way until asked for.
      node.append(el("details", { class: "tool-cards" },
        el("summary", { text: `${cards.length} message${cards.length === 1 ? "" : "s"}` }),
        cardList(cards)));
    }
  } else {
    node.append(el("div", { class: "notice-line", text: item.text || "" }));
  }

  if (item.change_request) node.append(changeRequestView(item.change_request));
  if (item.policy_proposal) node.append(proposalView(item.policy_proposal));
  if (item.time && item.role !== "tool" && item.role !== "notice") {
    node.append(el("div", { class: "item-time", text: formatTime(item.time) }));
  }
  return node;
}

function appendItem(item) {
  const stick = nearBottom();
  const existing = item.id === undefined || item.id === null
    ? null
    : [...items().children].find((node) => node.dataset.itemId === String(item.id));
  const node = renderItem(item);
  if (existing) existing.replaceWith(node);
  else items().append(node);
  if (stick) scrollToBottom();
}

function appendError(message) {
  items().append(el("div", { class: "item item-error", role: "alert" },
    el("div", { class: "notice-line error-text", text: message })));
  scrollToBottom();
}

function showEmpty() {
  clear(items()).append(el("div", { class: "empty", id: "chat-empty" },
    el("p", { text: "Ask a question about your email, for example:" }),
    el("ul", {},
      el("li", { text: "What do I need to reply to this week?" }),
      el("li", { text: "Summarise the unread messages in my inbox." }),
      el("li", { text: "Archive the newsletters from last month." }))));
}

/** Shows a conversation, or an empty one if `id` is null. */
export async function showConversation(id) {
  conversationId = id;
  handles.clear();
  if (id === null) {
    showEmpty();
    byId("chat-heading").textContent = "New conversation";
    return;
  }
  try {
    const conversation = await api.conversation(id);
    if (conversationId !== id) return; // the user moved on while it loaded
    byId("chat-heading").textContent = conversation.title || "Conversation";
    clear(items());
    for (const item of conversation.items || []) items().append(renderItem(item));
    if ((conversation.items || []).length === 0) showEmpty();
    scrollToBottom();
  } catch (error) {
    showError(error);
  }
}

function setBusy(value) {
  busy = value;
  byId("composer-send").disabled = value;
  byId("composer-send").textContent = value ? "Working…" : "Send";
  items().setAttribute("aria-busy", String(value));
}

async function submit(text) {
  if (busy) return;
  setBusy(true);
  byId("chat-empty")?.remove();

  // Shown until the backend confirms the message as an item of the conversation.
  const pending = el("div", { class: "item item-user pending" }, el("div", { class: "bubble", text }));
  items().append(pending);
  scrollToBottom();

  // The assistant's reply while it is being written.
  let streaming = null;
  let streamed = "";
  const endStream = () => {
    streaming?.remove();
    streaming = null;
    streamed = "";
  };

  try {
    if (conversationId === null) {
      const created = await api.newConversation();
      conversationId = created.id;
      byId("chat-heading").textContent = created.title || "Conversation";
      onConversationChanged(conversationId);
    }
    await api.sendMessage(conversationId, text, (event, data) => {
      if (event === "item") {
        if (data.role === "user") pending.remove();
        if (data.role === "assistant") endStream();
        appendItem(data);
      } else if (event === "delta") {
        const stick = nearBottom();
        if (!streaming) {
          streaming = el("div", { class: "item item-assistant streaming" },
            el("div", { class: "bubble markdown" }));
          items().append(streaming);
        }
        streamed += typeof data.text === "string" ? data.text : "";
        streaming.firstChild.replaceChildren(renderMarkdown(streamed, markdownOptions()));
        if (stick) scrollToBottom();
      } else if (event === "error") {
        endStream();
        appendError(data.message || "Something went wrong.");
      }
    });
  } catch (error) {
    if (error.name !== "AbortError") appendError(error.message);
  } finally {
    endStream();
    // If the message was never confirmed, keep it visible but marked.
    if (pending.isConnected) pending.classList.add("unconfirmed");
    setBusy(false);
    onConversationChanged(conversationId);
    byId("composer-text").focus();
  }
}

/** Fills the list of conversations in the sidebar. */
export async function refreshConversations() {
  await attempt(async () => {
    const { conversations } = await api.conversations();
    const list = clear(byId("conversation-list"));
    for (const conversation of conversations || []) {
      const current = conversation.id === conversationId;
      list.append(el("li", {},
        el("a", {
          class: `conversation${current ? " current" : ""}`,
          href: `#chat/${encodeURIComponent(conversation.id)}`,
          "aria-current": current ? "true" : null,
          title: formatTime(conversation.updated),
          text: conversation.title || "Conversation",
        })));
    }
    if ((conversations || []).length === 0) {
      list.append(el("li", { class: "hint", text: "No conversations yet." }));
    }
  });
}

export function initChat() {
  const form = byId("composer");
  const input = byId("composer-text");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = "";
    submit(text);
  });
  input.addEventListener("keydown", (event) => {
    // Enter sends; Shift+Enter makes a new line. Not while composing with an input method.
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  showEmpty();
}

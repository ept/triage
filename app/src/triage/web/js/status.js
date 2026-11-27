// The status area in the sidebar, notices, and the badges on the navigation.

import { api } from "./api.js";
import { byId, clear, el } from "./dom.js";
import { formatTime } from "./format.js";
import { attempt } from "./shared.js";

const IDLE_INTERVAL = 5000;
const SYNCING_INTERVAL = 1000;

// Notices the user has dismissed, by time and text.
const dismissed = new Set();
let timer = null;

function accountsView(status) {
  const list = el("ul", { class: "plain-list accounts" });
  for (const account of status.accounts || []) {
    const inbox = (status.counts || []).find(
      (c) => c.account === account.id && String(c.folder).toUpperCase() === "INBOX");
    const failure = (status.sync?.accounts || []).find((a) => a.account === account.id)?.error;
    list.append(el("li", { class: "account" },
      el("div", { class: "account-address", text: account.address || account.id }),
      inbox
        ? el("div", { class: "hint", text: `Inbox: ${inbox.total} messages, ${inbox.unread} unread` })
        : null,
      failure ? el("div", { class: "error-text", text: failure }) : null));
  }
  if ((status.accounts || []).length === 0) {
    list.append(el("li", { class: "hint", text: "No mail accounts are set up." }));
  }
  return list;
}

function syncView(sync) {
  const view = el("div", { class: "sync" });
  if (!sync) return view;
  if (sync.state === "running") {
    const total = sync.messages_total || 0;
    const done = Math.min(sync.messages_done || 0, total);
    view.append(
      el("div", { class: "hint", text: total ? `Fetching mail: ${done} of ${total}` : "Checking for mail…" }),
      el("progress", {
        id: "sync-progress",
        max: total || null,
        value: total ? done : null,
        "aria-label": "Mail download progress",
      }));
    const active = [];
    for (const account of sync.accounts || []) {
      for (const folder of account.folders || []) {
        if (folder.state === "fetching" || folder.state === "listing") active.push(folder.folder);
      }
    }
    if (active.length) view.append(el("div", { class: "hint", text: active.slice(0, 3).join(", ") }));
  } else if (sync.finished) {
    view.append(el("div", { class: "hint", text: `Mail checked ${formatTime(sync.finished)}` }));
  } else {
    view.append(el("div", { class: "hint", text: "Mail has not been fetched yet." }));
  }
  for (const error of (sync.errors || []).slice(0, 5)) {
    view.append(el("div", { class: "error-text", text: error }));
  }
  return view;
}

function llmView(llm) {
  if (llm?.available) {
    return el("div", { class: "llm ok" },
      el("span", { class: "dot ok", "aria-hidden": "true" }),
      el("span", { text: `Local model ready${llm.model ? `: ${llm.model}` : ""}` }));
  }
  return el("div", { class: "llm" },
    el("span", { class: "dot bad", "aria-hidden": "true" }),
    el("span", { text: "Local model not available" }),
    llm?.error ? el("div", { class: "error-text", text: llm.error }) : null);
}

function renderNotices(notices) {
  const container = clear(byId("notices"));
  for (const notice of notices || []) {
    const id = `${notice.time}|${notice.text}`;
    if (dismissed.has(id)) continue;
    container.append(el("div", { class: `notice notice-${notice.kind === "deletion_rate" ? "warning" : "info"}` },
      el("span", { class: "notice-text", text: notice.text || "" }),
      el("span", { class: "hint", text: formatTime(notice.time) }),
      el("button", {
        type: "button",
        class: "small",
        text: "Dismiss",
        on: {
          click: () => {
            dismissed.add(id);
            renderNotices(notices);
          },
        },
      })));
  }
}

function setBadge(id, count) {
  const badge = byId(id);
  badge.hidden = !(count > 0);
  badge.textContent = count > 0 ? String(count) : "";
  if (count > 0) badge.setAttribute("aria-label", `${count} waiting`);
}

function render(status) {
  clear(byId("status-body")).append(accountsView(status), syncView(status.sync), llmView(status.llm));
  byId("sync-now").disabled = status.sync?.state === "running";
  renderNotices(status.notices);
  setBadge("badge-coding", status.pending_change_requests);
  setBadge("badge-policy", status.pending_policy_proposals);
}

async function poll() {
  let interval = IDLE_INTERVAL;
  try {
    const status = await api.status();
    render(status);
    if (status.sync?.state === "running") interval = SYNCING_INTERVAL;
  } catch (error) {
    clear(byId("status-body")).append(el("div", { class: "error-text", text: error.message }));
  }
  timer = setTimeout(poll, interval);
}

export function refreshStatus() {
  clearTimeout(timer);
  return poll();
}

export function initStatus() {
  byId("sync-now").addEventListener("click", async () => {
    await attempt(() => api.sync());
    refreshStatus();
  });
  poll();
}

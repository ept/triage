// The activity view: everything that was changed in the user's mailboxes, with undo.

import { api } from "./api.js";
import { byId, clear, el } from "./dom.js";
import { formatTime } from "./format.js";
import { attempt } from "./shared.js";

function entryView(entry) {
  const actions = el("span", { class: "entry-actions" });
  if (entry.undone) {
    actions.append(el("span", { class: "state state-rejected", text: "undone" }));
  } else if (entry.undoable) {
    const button = el("button", {
      type: "button",
      class: "small",
      text: "Undo",
      "aria-label": `Undo: ${entry.summary || entry.op}`,
      on: {
        click: async () => {
          button.disabled = true;
          const ok = await attempt(() => api.undo(entry.seq));
          if (ok) await loadActivity();
          else button.disabled = false;
        },
      },
    });
    actions.append(button);
  }
  return el("li", { class: `entry${entry.error ? " has-error" : ""}`, dataset: { seq: entry.seq } },
    el("div", { class: "entry-main" },
      el("span", { class: "entry-summary", text: entry.summary || entry.op || "" }),
      actions),
    el("div", { class: "hint", text: [formatTime(entry.time), entry.account].filter(Boolean).join(" · ") }),
    entry.error ? el("div", { class: "error-text", text: `Failed: ${entry.error}` }) : null);
}

export async function loadActivity() {
  await attempt(async () => {
    const { entries } = await api.activity(0);
    const list = clear(byId("activity-list"));
    // Newest first.
    for (const entry of [...(entries || [])].reverse()) list.append(entryView(entry));
    if ((entries || []).length === 0) {
      list.append(el("li", { class: "hint", text: "Nothing has been changed yet." }));
    }
  });
}

export function initActivity() {
  byId("activity-refresh").addEventListener("click", loadActivity);
}

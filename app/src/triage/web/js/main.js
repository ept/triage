// Entry point: wires the views together and switches between them.
//
// The current view is kept in the fragment of the address (#chat/12, #activity, #policy,
// #coding), so that reloading the page returns to the same place.

import { loadActivity, initActivity } from "./activity.js";
import { api } from "./api.js";
import {
  currentConversation, initChat, refreshConversations, setConversationListener, showConversation,
} from "./chat.js";
import { initCoding, startCoding, stopCoding } from "./coding.js";
import { byId } from "./dom.js";
import { initPolicy, loadPolicy } from "./policy.js";
import { clearError } from "./shared.js";
import { initStatus } from "./status.js";

const VIEWS = ["chat", "activity", "policy", "coding"];

function parseRoute() {
  const [view, argument] = location.hash.replace(/^#/, "").split("/");
  if (!VIEWS.includes(view)) return { view: "chat", conversation: undefined };
  let conversation;
  if (view === "chat" && argument) {
    const decoded = decodeURIComponent(argument);
    // Conversation ids are numbers in the backend; keep other ids as they are.
    conversation = /^\d+$/.test(decoded) ? Number(decoded) : decoded;
  }
  return { view, conversation };
}

async function route() {
  const { view, conversation } = parseRoute();
  for (const name of VIEWS) byId(`view-${name}`).hidden = name !== view;
  for (const button of document.querySelectorAll("#nav .nav-item")) {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }

  if (view === "coding") startCoding();
  else stopCoding();

  if (view === "chat") {
    const wanted = conversation === undefined ? null : conversation;
    if (wanted !== currentConversation()) await showConversation(wanted);
    refreshConversations();
    byId("composer-text").focus();
  } else if (view === "activity") {
    await loadActivity();
  } else if (view === "policy") {
    await loadPolicy();
  }
}

function init() {
  initChat();
  initActivity();
  initPolicy();
  initCoding();
  initStatus();

  for (const button of document.querySelectorAll("#nav .nav-item")) {
    button.addEventListener("click", () => {
      const view = button.dataset.view;
      const id = currentConversation();
      const target = view === "chat" && id !== null ? `#chat/${encodeURIComponent(id)}` : `#${view}`;
      // Selecting the view that is already shown does not fire hashchange.
      if (location.hash === target) route();
      else location.hash = target;
    });
  }
  byId("new-conversation").addEventListener("click", async () => {
    await showConversation(null);
    if (location.hash === "#chat") route();
    else location.hash = "#chat";
  });
  byId("error-dismiss").addEventListener("click", clearError);

  // A conversation that was just created gets its own address, without reloading it.
  setConversationListener((id) => {
    if (id !== null && parseRoute().view === "chat") {
      history.replaceState(null, "", `#chat/${encodeURIComponent(id)}`);
    }
    refreshConversations();
  });

  window.addEventListener("hashchange", route);
  route();
  reportTimeZone();
}

// The app runs in a container that does not know where the user is. It needs their time zone
// to write the date of a quoted message, in a reply, the way their mail program would.
async function reportTimeZone() {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (zone) await api.saveSettings({ time_zone: zone });
  } catch {
    // Not worth a message: replies then state the time in UTC.
  }
}

init();

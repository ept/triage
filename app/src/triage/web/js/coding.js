// The "App changes" view: requests to the coding agent and what the agent says.
//
// The agent's output comes from a sandbox with internet access. It is shown as plain text only:
// no Markdown, no links, nothing interpreted.

import { api } from "./api.js";
import { byId, clear, el } from "./dom.js";
import { formatTime } from "./format.js";
import { attempt, changeRequestView } from "./shared.js";

const LONG_POLL_SECONDS = 25;

let lastSeq = 0;
let lastKind = null;
let polling = null; // AbortController of the running long poll, if the view is shown

function renderRequests(requests) {
  const list = clear(byId("coding-requests"));
  // Newest first.
  for (const request of [...(requests || [])].reverse()) {
    list.append(el("li", {}, changeRequestView(request)));
  }
  if ((requests || []).length === 0) {
    list.append(el("li", { class: "hint", text: "No requests yet." }));
  }
}

function appendEvent(event) {
  const output = byId("coding-output");
  byId("coding-empty")?.remove();
  const stick = output.scrollHeight - output.scrollTop - output.clientHeight < 80;
  const kind = ["text", "status", "question", "done"].includes(event.kind) ? event.kind : "text";
  const node = el("div", { class: `coding-event coding-kind-${kind}`, dataset: { seq: event.seq } });
  if (kind === "question") {
    node.append(el("div", { class: "coding-label", text: "The coding agent asks:" }));
  }
  if (kind === "done") {
    node.append(el("div", { class: "coding-label", text: `Finished · ${formatTime(event.time)}` }));
  }
  if (event.text) node.append(el("pre", { class: "coding-text", text: event.text }));
  output.append(node);
  if (stick) output.scrollTop = output.scrollHeight;
}

function updateForm() {
  const replying = lastKind === "question";
  byId("coding-label").textContent = replying
    ? "Reply to the coding agent"
    : "Ask for a change to the app";
  byId("coding-form").dataset.kind = replying ? "reply" : "feature";
}

function apply(result) {
  for (const event of result.events || []) {
    if (typeof event.seq === "number" && event.seq <= lastSeq) continue;
    appendEvent(event);
    lastKind = event.kind;
    if (typeof event.seq === "number") lastSeq = event.seq;
  }
  if (typeof result.last_seq === "number" && result.last_seq > lastSeq) lastSeq = result.last_seq;
  renderRequests(result.requests);
  updateForm();
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function pollLoop(controller) {
  let wait = 0; // the first request returns at once, later ones wait for news
  while (!controller.signal.aborted) {
    const started = Date.now();
    try {
      apply(await api.coding(lastSeq, wait, controller.signal));
      wait = LONG_POLL_SECONDS;
      // A backend that answers early instead of waiting must not be asked in a tight loop.
      if (Date.now() - started < 1000) await sleep(1000);
    } catch (error) {
      if (error.name === "AbortError") return;
      await sleep(3000);
    }
  }
}

/** Starts following the coding agent; called when the view is shown. */
export function startCoding() {
  if (polling) return;
  polling = new AbortController();
  pollLoop(polling);
}

/** Stops following; called when the view is left. */
export function stopCoding() {
  polling?.abort();
  polling = null;
}

export function initCoding() {
  const form = byId("coding-form");
  const input = byId("coding-text");
  byId("coding-output").append(
    el("p", { class: "hint", id: "coding-empty", text: "Nothing from the coding agent yet." }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    const button = byId("coding-send");
    button.disabled = true;
    const ok = await attempt(() => api.codingRequest(form.dataset.kind || "feature", text));
    button.disabled = false;
    if (ok) {
      input.value = "";
      await attempt(async () => apply(await api.coding(lastSeq, 0)));
    }
  });
  updateForm();
}

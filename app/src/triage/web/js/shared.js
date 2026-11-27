// Pieces used by several views: the error bar, state badges, diffs, and policy proposals.

import { api } from "./api.js";
import { byId, el } from "./dom.js";
import { diffLines, formatTime } from "./format.js";

/** Shows an error at the top of the main pane until it is dismissed. */
export function showError(error) {
  if (error && error.name === "AbortError") return;
  const message = error && error.message ? error.message : String(error);
  byId("error-text").textContent = message;
  byId("error-bar").hidden = false;
}

export function clearError() {
  byId("error-bar").hidden = true;
  byId("error-text").textContent = "";
}

/** Runs an action, reporting a failure in the error bar. Returns whether it succeeded. */
export async function attempt(action) {
  try {
    await action();
    return true;
  } catch (error) {
    showError(error);
    return false;
  }
}

const STATE_LABELS = {
  pending: "waiting for approval",
  approved: "approved",
  rejected: "rejected",
  expired: "expired",
  accepted: "accepted",
};

/** A small label for the state of a change request or proposal. */
export function stateBadge(state) {
  const known = Object.hasOwn(STATE_LABELS, state);
  return el("span", {
    class: `state state-${known ? state : "unknown"}`,
    text: known ? STATE_LABELS[state] : String(state),
  });
}

/** A unified diff, one element per line so that additions and removals can be coloured. */
export function diffView(diff) {
  const view = el("div", { class: "diff", role: "group", "aria-label": "Proposed changes" });
  for (const { kind, text } of diffLines(diff)) {
    view.append(el("div", { class: `diff-line diff-${kind}`, text: text === "" ? " " : text }));
  }
  return view;
}

/**
 * A proposal by the local model to change the policy: the diff, and buttons to accept or reject
 * it while it is pending. `onDecided(accepted)` is called after the user's decision was saved.
 */
export function proposalView(proposal, onDecided) {
  const status = el("div", { class: "row" }, stateBadge(proposal.state));
  const view = el(
    "div",
    { class: "card proposal", dataset: { proposal: proposal.id } },
    el("div", { class: "card-title" }, "Proposed change to your policy",
      proposal.time ? el("span", { class: "hint", text: ` · ${formatTime(proposal.time)}` }) : null),
    diffView(proposal.diff),
    status,
  );
  if (proposal.state === "pending") {
    const decide = async (accept) => {
      accept_button.disabled = reject_button.disabled = true;
      const ok = await attempt(() => api.decideProposal(proposal.id, accept));
      if (!ok) {
        accept_button.disabled = reject_button.disabled = false;
        return;
      }
      proposal.state = accept ? "accepted" : "rejected";
      status.replaceChildren(stateBadge(proposal.state));
      if (onDecided) onDecided(accept);
    };
    const accept_button = el("button", {
      type: "button", class: "small primary", text: "Accept", on: { click: () => decide(true) },
    });
    const reject_button = el("button", {
      type: "button", class: "small", text: "Reject", on: { click: () => decide(false) },
    });
    status.append(accept_button, reject_button);
  }
  return view;
}

const KIND_LABELS = {
  feature: "Request for a change to the app",
  bug: "Bug report",
  reply: "Reply to the coding agent",
};

/**
 * A message that the app wants to send to the coding agent. It is shown in full, because the
 * user is asked to approve exactly this text, in the trusted window outside this page.
 */
export function changeRequestView(request) {
  const explanation = {
    pending:
      "Waiting for your approval in the Email Triage window outside this page. " +
      "It is sent to a remote model only once you approve it there.",
    approved: "You approved this in the Email Triage window. It was sent to the coding agent.",
    rejected: "You rejected this. It was not sent anywhere.",
    expired: "This was not approved in time. It was not sent anywhere.",
  }[request.state];
  return el(
    "div",
    { class: "card change-request", dataset: { request: request.id } },
    el("div", { class: "card-title" },
      Object.hasOwn(KIND_LABELS, request.kind) ? KIND_LABELS[request.kind] : "Message to the coding agent",
      " ", stateBadge(request.state),
      request.time ? el("span", { class: "hint", text: ` · ${formatTime(request.time)}` }) : null),
    el("pre", { class: "request-text", text: request.text }),
    explanation ? el("p", { class: "hint", text: explanation }) : null,
  );
}

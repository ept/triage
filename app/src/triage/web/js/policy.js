// The policy view: the user's rules for their email, proposals to change them, and history.

import { api } from "./api.js";
import { byId, clear, el } from "./dom.js";
import { formatTime } from "./format.js";
import { attempt, proposalView } from "./shared.js";

// The text as last loaded or saved, to tell whether the editor has unsaved changes.
let savedText = "";

const editor = () => byId("policy-text");
const isDirty = () => editor().value !== savedText;

function setSaved(text, updated) {
  savedText = text;
  editor().value = text;
  byId("policy-updated").textContent = updated ? `Last changed ${formatTime(updated)}` : "";
  updateSaveButton();
}

function updateSaveButton() {
  byId("policy-save").disabled = !isDirty();
}

async function loadHistory() {
  const { versions } = await api.policyHistory();
  const list = clear(byId("policy-history"));
  for (const version of versions || []) {
    list.append(el("li", {},
      el("button", {
        type: "button",
        class: "link-button",
        dataset: { version: version.id },
        text: `${formatTime(version.time)} — ${version.summary || "change"}`,
        on: { click: () => showVersion(version) },
      })));
  }
  if ((versions || []).length === 0) {
    list.append(el("li", { class: "hint", text: "No earlier versions." }));
  }
}

async function showVersion(version) {
  await attempt(async () => {
    const { text } = await api.policyVersion(version.id);
    byId("policy-version-title").textContent = `Version of ${formatTime(version.time)}`;
    byId("policy-version-text").textContent = text;
    byId("policy-version").hidden = false;
    byId("policy-restore").onclick = () => {
      // Restoring only loads the text into the editor; the user still has to save.
      editor().value = text;
      updateSaveButton();
      editor().focus();
    };
  });
}

export async function loadPolicy() {
  await attempt(async () => {
    const policy = await api.policy();
    // Do not throw away what the user is typing.
    if (!isDirty()) setSaved(policy.text || "", policy.updated);
    const proposals = clear(byId("policy-proposals"));
    for (const proposal of policy.proposals || []) {
      if (proposal.state !== "pending") continue;
      proposals.append(proposalView(proposal, async (accepted) => {
        if (accepted) {
          savedText = editor().value; // the accepted text replaces the editor's content
          await loadPolicy();
        }
      }));
    }
    await loadHistory();
    const settings = await api.settings();
    byId("policy-automatic").checked = settings.apply_policy_automatically === true;
  });
}

export function initPolicy() {
  editor().addEventListener("input", updateSaveButton);
  byId("policy-automatic").addEventListener("change", async (event) => {
    const box = event.target;
    const wanted = box.checked;
    box.disabled = true;
    let stored = !wanted; // if saving fails, the box goes back to what it was
    await attempt(async () => {
      const saved = await api.saveSettings({ apply_policy_automatically: wanted });
      stored = saved.apply_policy_automatically === true;
    });
    box.checked = stored;
    box.disabled = false;
  });
  byId("policy-save").addEventListener("click", async () => {
    const text = editor().value;
    const ok = await attempt(() => api.savePolicy(text));
    if (ok) {
      savedText = text;
      updateSaveButton();
      await loadPolicy();
    }
  });
  byId("policy-version-close").addEventListener("click", () => {
    byId("policy-version").hidden = true;
  });
  updateSaveButton();
}

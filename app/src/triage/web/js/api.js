// Client for the app's backend. All URLs are relative, because the page is reached through a
// relay whose address the page does not know.

export class ApiError extends Error {
  constructor(message, code = "error", status = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

async function errorFrom(response) {
  let message = `The request failed (${response.status}).`;
  let code = "error";
  try {
    const body = await response.json();
    if (body && body.error) {
      if (typeof body.error.message === "string") message = body.error.message;
      if (typeof body.error.code === "string") code = body.error.code;
    }
  } catch {
    // Not JSON; keep the generic message.
  }
  return new ApiError(message, code, response.status);
}

async function send(method, path, body, signal) {
  const options = { method, signal, headers: {}, cache: "no-store" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("The app cannot be reached.", "unreachable");
  }
  if (!response.ok) throw await errorFrom(response);
  return response;
}

async function json(method, path, body, signal) {
  const response = await send(method, path, body, signal);
  try {
    return await response.json();
  } catch {
    throw new ApiError("The app sent an answer that could not be understood.", "bad_response");
  }
}

const segment = (value) => encodeURIComponent(String(value));

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/**
 * Incremental parser for a `text/event-stream` body. `feed(text)` returns the events that are
 * complete so far, each as `{event, data}` with `data` still a string.
 */
export class EventStreamParser {
  constructor() {
    this.buffer = "";
  }

  feed(text) {
    this.buffer += text.replace(/\r\n?/g, "\n");
    const events = [];
    let end;
    while ((end = this.buffer.indexOf("\n\n")) !== -1) {
      const block = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      let event = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let value = colon === -1 ? "" : line.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") event = value;
        else if (field === "data") data.push(value);
      }
      if (data.length > 0) events.push({ event, data: data.join("\n") });
    }
    return events;
  }
}

export const api = {
  status: (signal) => json("GET", "api/status", undefined, signal),
  sync: () => json("POST", "api/sync", {}),

  conversations: () => json("GET", "api/conversations"),
  newConversation: () => json("POST", "api/conversations", {}),
  conversation: (id) => json("GET", `api/conversations/${segment(id)}`),

  /**
   * Sends a chat message and reads the streamed answer. `onEvent(name, data)` is called for
   * every event, with `data` parsed. Resolves when the stream ends.
   */
  async sendMessage(id, text, onEvent, signal) {
    const response = await send("POST", `api/conversations/${segment(id)}/messages`, { text }, signal);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const parser = new EventStreamParser();
    for (;;) {
      const { value, done } = await reader.read();
      const chunk = done ? decoder.decode() + "\n\n" : decoder.decode(value, { stream: true });
      for (const { event, data } of parser.feed(chunk)) {
        let parsed;
        try {
          parsed = JSON.parse(data);
        } catch {
          continue;
        }
        onEvent(event, parsed);
      }
      if (done) break;
    }
  },

  message: (key) => json("GET", `api/messages/${segment(key)}`),
  async messageHtml(key) {
    const response = await send("GET", `api/messages/${segment(key)}/html`);
    return response.text();
  },
  search: (params) => json("GET", `api/search${query(params)}`),

  settings: () => json("GET", "api/settings"),
  saveSettings: (changes) => json("PUT", "api/settings", changes),

  activity: (after = 0) => json("GET", `api/activity${query({ after })}`),
  undo: (seq) => json("POST", `api/activity/${segment(seq)}/undo`, {}),

  policy: () => json("GET", "api/policy"),
  savePolicy: (text) => json("PUT", "api/policy", { text }),
  policyHistory: () => json("GET", "api/policy/history"),
  policyVersion: (id) => json("GET", `api/policy/history/${segment(id)}`),
  decideProposal: (id, accept) => json("POST", `api/policy/proposals/${segment(id)}`, { accept }),

  coding: (after, wait, signal) => json("GET", `api/coding${query({ after, wait })}`, undefined, signal),
  codingRequest: (kind, text) => json("POST", "api/coding/requests", { kind, text }),
};

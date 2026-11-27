// Formatting of values for display. Pure functions, no DOM.

/** A date and time in the user's locale, or "" if the value is missing or not a date. */
export function formatTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  const options = sameDay
    ? { hour: "2-digit", minute: "2-digit" }
    : { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
  return date.toLocaleString(undefined, options);
}

/** "Name <address>", or whichever of the two is present. */
export function formatAddress(address) {
  if (!address) return "";
  const name = (address.name || "").trim();
  const addr = (address.address || "").trim();
  if (name && addr) return `${name} <${addr}>`;
  return name || addr;
}

export function formatAddresses(list) {
  return (list || []).map(formatAddress).filter(Boolean).join(", ");
}

/** A short form for lists: the name if there is one, else the address. */
export function shortSender(list) {
  const first = (list || [])[0];
  if (!first) return "(unknown sender)";
  const label = (first.name || "").trim() || (first.address || "").trim() || "(unknown sender)";
  return list.length > 1 ? `${label} +${list.length - 1}` : label;
}

export function formatSize(bytes) {
  if (typeof bytes !== "number" || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Classifies the lines of a unified diff for colouring.
 * Returns [{kind: "add"|"remove"|"hunk"|"meta"|"context", text}].
 */
export function diffLines(diff) {
  return String(diff || "")
    .split("\n")
    .map((text) => {
      let kind = "context";
      if (text.startsWith("+++") || text.startsWith("---")) kind = "meta";
      else if (text.startsWith("@@")) kind = "hunk";
      else if (text.startsWith("+")) kind = "add";
      else if (text.startsWith("-")) kind = "remove";
      return { kind, text };
    });
}

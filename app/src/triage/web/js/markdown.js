// A small Markdown renderer for the assistant's replies.
//
// The text comes from a model that has read email, so it is untrusted. The renderer therefore
// supports only a fixed set of constructs and builds DOM nodes from them directly:
//
//   paragraphs, line breaks, headings, bullet and numbered lists, fenced code blocks,
//   **bold**, *italic*, `code`
//
// Two things are deliberately NOT rendered:
//
//   * Images. `![alt](url)` stays as the literal text. (An image whose URL encodes private data
//     would leak it the moment it was displayed.)
//   * Links. `[text](url)` becomes "text (url)" as plain text, with the full URL visible, and a
//     bare URL stays plain text. Nothing here is clickable.
//
// A token such as `[m3]` refers to a message; if the caller knows the handle it becomes a button.
//
// Parsing (`parseMarkdown`) is separate from rendering (`renderMarkdown`) and uses no DOM, so
// that it can be tested on its own.

const MAX_LENGTH = 200_000;
const MAX_INLINE_DEPTH = 4;

const FENCE = /^\s{0,3}```/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const BULLET = /^\s{0,6}[-*+]\s+(.*)$/;
const NUMBERED = /^\s{0,6}(\d{1,9})[.)]\s+(.*)$/;

/** Parses Markdown into a list of blocks. */
export function parseMarkdown(text) {
  text = String(text ?? "").replace(/\r\n?/g, "\n");
  if (text.length > MAX_LENGTH) {
    return [{ type: "paragraph", inlines: [{ type: "text", text }] }];
  }
  const lines = text.split("\n");
  const blocks = [];
  let paragraph = [];

  const flush = () => {
    if (paragraph.length === 0) return;
    const inlines = [];
    paragraph.forEach((line, index) => {
      if (index > 0) inlines.push({ type: "break" });
      inlines.push(...parseInline(line));
    });
    blocks.push({ type: "paragraph", inlines });
    paragraph = [];
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (FENCE.test(line)) {
      flush();
      const code = [];
      i++;
      while (i < lines.length && !FENCE.test(lines[i])) code.push(lines[i++]);
      blocks.push({ type: "code", text: code.join("\n") });
      continue;
    }
    if (line.trim() === "") {
      flush();
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      flush();
      blocks.push({ type: "heading", level: heading[1].length, inlines: parseInline(heading[2]) });
      continue;
    }
    const bullet = BULLET.exec(line);
    const numbered = bullet ? null : NUMBERED.exec(line);
    if (bullet || numbered) {
      flush();
      const ordered = Boolean(numbered);
      const pattern = ordered ? NUMBERED : BULLET;
      const items = [];
      let start = ordered ? Number(numbered[1]) : null;
      while (i < lines.length) {
        const match = pattern.exec(lines[i]);
        if (!match) break;
        items.push(parseInline(match[ordered ? 2 : 1]));
        i++;
      }
      i--;
      blocks.push({ type: "list", ordered, start, items });
      continue;
    }
    paragraph.push(line.trim());
  }
  flush();
  return blocks;
}

// Inline constructs, each tried at the current position.
// The repetitions are bounded so that text made of many unclosed openers ("[[[[…", "** ** …")
// cannot make matching take time quadratic in its length.
const CODE = /`([^`\n]{1,2000})`/y;
const BOLD = /\*\*(?=\S)([^\n]{0,1000}?\S)\*\*/y;
const ITALIC = /\*(?=[^\s*])([^*\n]{0,1000}?[^\s*])\*(?!\*)/y;
const IMAGE = /!\[[^\]\n]{0,500}\]\([^)\n]{0,4000}\)/y;
const LINK = /\[([^\]\n]{1,500})\]\(([^)\s]{1,4000})(?:\s+"[^"\n]{0,500}")?\)/y;
const HANDLE = /\[(m\d{1,6})\]/y;
const SPECIAL = /[`*!\[]/g;

/** Parses the inline constructs of one line. */
export function parseInline(text, depth = 0) {
  const out = [];
  let buffer = "";
  const pushText = () => {
    if (buffer) out.push({ type: "text", text: buffer });
    buffer = "";
  };
  const tryAt = (pattern, index) => {
    pattern.lastIndex = index;
    return pattern.exec(text);
  };

  let i = 0;
  while (i < text.length) {
    // Jump to the next character that could start a construct.
    SPECIAL.lastIndex = i;
    const next = SPECIAL.exec(text);
    if (!next) {
      buffer += text.slice(i);
      break;
    }
    buffer += text.slice(i, next.index);
    i = next.index;
    const char = text[i];
    let match;

    if (char === "`" && (match = tryAt(CODE, i))) {
      pushText();
      out.push({ type: "code", text: match[1] });
    } else if (char === "!" && (match = tryAt(IMAGE, i))) {
      // Images are never rendered; keep the source text.
      buffer += match[0];
    } else if (char === "[" && (match = tryAt(HANDLE, i))) {
      pushText();
      out.push({ type: "handle", handle: match[1] });
    } else if (char === "[" && (match = tryAt(LINK, i))) {
      pushText();
      out.push({ type: "link", text: match[1], url: match[2] });
    } else if (char === "*" && depth < MAX_INLINE_DEPTH && (match = tryAt(BOLD, i))) {
      pushText();
      out.push({ type: "bold", children: parseInline(match[1], depth + 1) });
    } else if (char === "*" && depth < MAX_INLINE_DEPTH && (match = tryAt(ITALIC, i))) {
      pushText();
      out.push({ type: "italic", children: parseInline(match[1], depth + 1) });
    } else {
      buffer += char;
      i++;
      continue;
    }
    i += match[0].length;
  }
  pushText();
  return out;
}

/** The text that a link is shown as: its label followed by the full URL. */
export function linkText(link) {
  return link.text === link.url ? link.url : `${link.text} (${link.url})`;
}

function renderInlines(inlines, options) {
  const nodes = [];
  for (const inline of inlines) {
    switch (inline.type) {
      case "text":
        nodes.push(document.createTextNode(inline.text));
        break;
      case "break":
        nodes.push(document.createElement("br"));
        break;
      case "code": {
        const code = document.createElement("code");
        code.textContent = inline.text;
        nodes.push(code);
        break;
      }
      case "bold":
      case "italic": {
        const node = document.createElement(inline.type === "bold" ? "strong" : "em");
        node.append(...renderInlines(inline.children, options));
        nodes.push(node);
        break;
      }
      case "link": {
        // Not an anchor: nothing in model output is clickable.
        const span = document.createElement("span");
        span.className = "link-text";
        span.textContent = linkText(inline);
        nodes.push(span);
        break;
      }
      case "handle": {
        const card = options.handles?.get(inline.handle);
        if (card && options.onHandle) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "handle";
          button.textContent = `[${inline.handle}]`;
          button.title = card.subject || "";
          button.dataset.handle = inline.handle;
          button.addEventListener("click", () => options.onHandle(card));
          nodes.push(button);
        } else {
          nodes.push(document.createTextNode(`[${inline.handle}]`));
        }
        break;
      }
    }
  }
  return nodes;
}

/**
 * Renders Markdown into a document fragment.
 *
 * `options.handles` is a Map from message handle ("m3") to message card, and
 * `options.onHandle(card)` is called when the button for a handle is pressed.
 */
export function renderMarkdown(text, options = {}) {
  const fragment = document.createDocumentFragment();
  for (const block of parseMarkdown(text)) {
    let node;
    switch (block.type) {
      case "heading":
        // Headings in a reply are small; h4 to h6 keep the page outline intact.
        node = document.createElement(`h${Math.min(6, block.level + 3)}`);
        node.append(...renderInlines(block.inlines, options));
        break;
      case "code": {
        node = document.createElement("pre");
        const code = document.createElement("code");
        code.textContent = block.text;
        node.append(code);
        break;
      }
      case "list":
        node = document.createElement(block.ordered ? "ol" : "ul");
        if (block.ordered && block.start > 1) node.setAttribute("start", String(block.start));
        for (const item of block.items) {
          const li = document.createElement("li");
          li.append(...renderInlines(item, options));
          node.append(li);
        }
        break;
      default:
        node = document.createElement("p");
        node.append(...renderInlines(block.inlines, options));
    }
    fragment.append(node);
  }
  return fragment;
}

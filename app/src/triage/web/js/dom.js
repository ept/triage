// Small helpers for building DOM nodes.
//
// Everything the backend sends is untrusted (it may quote email content, or come from the coding
// sandbox), so nodes are only ever built with createElement and text nodes. Nothing in the app
// hands a string to the browser's HTML parser (a test checks this).

/**
 * Creates an element.
 *
 * `props` may contain `class`, `text`, `dataset` (an object), `on` (an object of event
 * listeners), and any other key, which is set as an attribute (`true` sets an empty attribute,
 * `false`/`null`/`undefined` leave it out). Children are nodes or strings; strings become text
 * nodes.
 */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "on") {
      for (const [event, handler] of Object.entries(value)) node.addEventListener(event, handler);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export const byId = (id) => document.getElementById(id);

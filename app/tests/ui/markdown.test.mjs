// Unit tests for the parts of the web interface that need no browser.
// Run with: node --test tests/ui/

import assert from "node:assert/strict";
import { test } from "node:test";

import { EventStreamParser } from "../../src/triage/web/js/api.js";
import { diffLines, formatAddress, formatSize, shortSender } from "../../src/triage/web/js/format.js";
import { linkText, parseInline, parseMarkdown } from "../../src/triage/web/js/markdown.js";

const text = (value) => ({ type: "text", text: value });

test("paragraphs and line breaks", () => {
  assert.deepEqual(parseMarkdown("one\ntwo\n\nthree"), [
    { type: "paragraph", inlines: [text("one"), { type: "break" }, text("two")] },
    { type: "paragraph", inlines: [text("three")] },
  ]);
  assert.deepEqual(parseMarkdown(""), []);
  assert.deepEqual(parseMarkdown(null), []);
  assert.deepEqual(parseMarkdown("a\r\nb"), parseMarkdown("a\nb"));
});

test("headings, lists and code blocks", () => {
  const blocks = parseMarkdown("# Title\n\n- one\n- two\n\n3. three\n4. four\n\n```js\nlet a = '<b>';\n```\nafter");
  assert.deepEqual(blocks.map((b) => b.type), ["heading", "list", "list", "code", "paragraph"]);
  assert.equal(blocks[0].level, 1);
  assert.deepEqual(blocks[0].inlines, [text("Title")]);
  assert.equal(blocks[1].ordered, false);
  assert.deepEqual(blocks[1].items, [[text("one")], [text("two")]]);
  assert.equal(blocks[2].ordered, true);
  assert.equal(blocks[2].start, 3);
  assert.equal(blocks[3].text, "let a = '<b>';");
  // An unterminated fence takes the rest of the text.
  assert.deepEqual(parseMarkdown("```\nopen"), [{ type: "code", text: "open" }]);
  // "#hashtag" and "1.5" are not a heading or a list.
  assert.equal(parseMarkdown("#hashtag")[0].type, "paragraph");
  assert.equal(parseMarkdown("1.5 litres")[0].type, "paragraph");
});

test("inline formatting", () => {
  assert.deepEqual(parseInline("a **b** *c* `d`"), [
    text("a "),
    { type: "bold", children: [text("b")] },
    text(" "),
    { type: "italic", children: [text("c")] },
    text(" "),
    { type: "code", text: "d" },
  ]);
  assert.deepEqual(parseInline("**a *b* c**"), [
    { type: "bold", children: [text("a "), { type: "italic", children: [text("b")] }, text(" c")] },
  ]);
  // Things that only look like formatting stay as they are.
  for (const plain of ["2 * 3 * 4", "a ** b", "snake_case_name", "*", "**", "`", "a*b", "5 > 3 [x]"]) {
    assert.deepEqual(parseInline(plain), [text(plain)], plain);
  }
  // Code is not parsed any further.
  assert.deepEqual(parseInline("`**x** [m1]`"), [{ type: "code", text: "**x** [m1]" }]);
});

test("images are never rendered", () => {
  for (const source of [
    "![pic](https://evil.example/?q=secret)",
    "![](x)",
    "![a b](data:image/png;base64,AAAA)",
  ]) {
    assert.deepEqual(parseInline(source), [text(source)], source);
  }
  assert.deepEqual(parseInline("see ![a](b) and **c**"), [
    text("see ![a](b) and "),
    { type: "bold", children: [text("c")] },
  ]);
});

test("links become text that shows the address", () => {
  const [link] = parseInline("[click](https://example.com/a?b=c)");
  assert.deepEqual(link, { type: "link", text: "click", url: "https://example.com/a?b=c" });
  assert.equal(linkText(link), "click (https://example.com/a?b=c)");
  assert.equal(linkText({ text: "https://x.example", url: "https://x.example" }), "https://x.example");
  assert.equal(parseInline("[x](javascript:alert(1))")[0].url, "javascript:alert(1");
  // Bare addresses are plain text.
  assert.deepEqual(parseInline("see https://example.com/x"), [text("see https://example.com/x")]);
  // No inline type can carry markup: every leaf is text, code, a handle, or a link shown as text.
  const kinds = new Set();
  const walk = (inlines) => inlines.forEach((i) => (kinds.add(i.type), i.children && walk(i.children)));
  walk(parseInline("<img src=x onerror=1> **<script>** [a](b) ![c](d) [m1] `<b>`"));
  assert.deepEqual([...kinds].sort(), ["bold", "code", "handle", "link", "text"]);
});

test("message handles", () => {
  assert.deepEqual(parseInline("see [m3] and [m12]."), [
    text("see "),
    { type: "handle", handle: "m3" },
    text(" and "),
    { type: "handle", handle: "m12" },
    text("."),
  ]);
  for (const plain of ["[m]", "[x3]", "[m3", "[m1234567]", "[ m3 ]"]) {
    assert.deepEqual(parseInline(plain), [text(plain)], plain);
  }
});

test("pathological input is handled quickly", () => {
  const started = Date.now();
  parseMarkdown("*".repeat(20000));
  parseMarkdown("[".repeat(20000));
  parseMarkdown("**a ".repeat(5000));
  parseMarkdown("`".repeat(20000));
  parseMarkdown("![".repeat(10000));
  parseMarkdown(("- " + "*x ".repeat(50) + "\n").repeat(500));
  assert.ok(Date.now() - started < 2000, `took ${Date.now() - started} ms`);
  // Very long text is not parsed at all.
  const huge = parseMarkdown("**x** ".repeat(40000));
  assert.equal(huge.length, 1);
  assert.equal(huge[0].inlines.length, 1);
});

test("event stream parsing", () => {
  const parser = new EventStreamParser();
  assert.deepEqual(parser.feed("event: item\ndata: {\"a\":"), []);
  assert.deepEqual(parser.feed("1}\n\nevent: delta\r\ndata: x\r\n\r\n: comment\n\n"), [
    { event: "item", data: '{"a":1}' },
    { event: "delta", data: "x" },
  ]);
  assert.deepEqual(parser.feed("data: one\ndata: two\n\n"), [{ event: "message", data: "one\ntwo" }]);
  assert.deepEqual(parser.feed("event: done\ndata: {}"), []);
  assert.deepEqual(parser.feed("\n\n"), [{ event: "done", data: "{}" }]);
});

test("formatting helpers", () => {
  assert.equal(formatAddress({ name: "Kim Placeholder", address: "kim@example.net" }), "Kim Placeholder <kim@example.net>");
  assert.equal(formatAddress({ name: "", address: "kim@example.net" }), "kim@example.net");
  assert.equal(formatAddress(null), "");
  assert.equal(shortSender([{ name: "Kim", address: "k@example.net" }, { name: "Lee", address: "l@example.com" }]), "Kim +1");
  assert.equal(shortSender([]), "(unknown sender)");
  assert.equal(formatSize(512), "512 B");
  assert.equal(formatSize(48213), "47 KB");
  assert.equal(formatSize(5 * 1024 * 1024), "5.0 MB");
  assert.deepEqual(diffLines("--- a\n+++ b\n@@ -1 +1 @@\n same\n-old\n+new").map((l) => l.kind),
    ["meta", "meta", "hunk", "context", "remove", "add"]);
});

import assert from "node:assert/strict";
import { test } from "node:test";

import { escapeHtml, renderSafeMarkdown } from "../../extension/src/webview/safeMarkdown";

test("escape happens before Markdown recognition", () => {
  const rendered = renderSafeMarkdown("<script>alert(1)</script> **safe** <img src=x onerror=boom>");
  assert.match(rendered, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(rendered, /<strong>safe<\/strong>/);
  assert.match(rendered, /&lt;img src=x onerror=boom&gt;/);
  assert.doesNotMatch(rendered, /<script\b|<img\b|<[a-z][^>]*\sonerror=/i);
});

test("only http(s) links become anchors and attributes cannot be injected", () => {
  const rendered = renderSafeMarkdown([
    "[safe](https://example.test/a?x=1&y=2)",
    "[js](javascript:alert(1))",
    "[data](data:text/html,boom)",
    "[file](file:///secret)",
    "[relative](/private)",
    "[protocol-relative](//example.test/private)",
    "[quote](https://example.test/&quot;onmouseover=&quot;boom)",
  ].join("\n\n"));
  const anchors = rendered.match(/<a\b/g);
  assert.ok(anchors);
  assert.equal(anchors.length, 2);
  assert.match(rendered, /href="https:\/\/example\.test\/a\?x=1&amp;y=2"/);
  assert.doesNotMatch(rendered, /href="(?:javascript|data|file):/i);
  assert.doesNotMatch(rendered, /\sonmouseover=/i);
  assert.match(rendered, /target="_blank" rel="noopener noreferrer"/);
});

test("the exact supported block and inline subset renders without tables or raw styles", () => {
  const rendered = renderSafeMarkdown([
    "# H1",
    "## H2",
    "### H3",
    "",
    "**bold** *em* ~~gone~~ `inline`",
    "",
    "- one",
    "- two",
    "",
    "1. first",
    "2. second",
    "",
    "> quote",
    "",
    "---",
    "",
    "```ts",
    "const value = '<unsafe>';",
    "```",
    "",
    "| not | a table |",
    "| --- | --- |",
  ].join("\n"));
  for (const fragment of ["<h1>", "<h2>", "<h3>", "<strong>", "<em>", "<del>", "<code>", "<ul>", "<ol>", "<blockquote>", "<hr>", "class=\"copy-code\""]) {
    assert.ok(rendered.includes(fragment), `missing ${fragment}`);
  }
  assert.match(rendered, /<div class="code-block"><pre><code data-language="ts">/);
  assert.match(rendered, /&lt;unsafe&gt;/);
  assert.doesNotMatch(rendered, /<table\b|<style\b|\sstyle=/i);
  const allowed = new Set(["a", "blockquote", "br", "button", "code", "del", "div", "em", "h1", "h2", "h3", "hr", "li", "ol", "p", "pre", "strong", "ul"]);
  for (const match of rendered.matchAll(/<\/?([a-z][a-z0-9-]*)\b/gi)) {
    assert.ok(allowed.has(match[1].toLowerCase()), `unexpected tag ${match[1]}`);
  }
});

test("fenced and inline code remain escaped literal text", () => {
  const rendered = renderSafeMarkdown("`<b>x</b>`\n\n```html\n</code><script>x</script>\n```");
  assert.doesNotMatch(rendered, /<script\b|<b\b/i);
  assert.match(rendered, /&lt;b&gt;x&lt;\/b&gt;/);
  assert.match(rendered, /&lt;\/code&gt;&lt;script&gt;x&lt;\/script&gt;/);
});

test("underscore emphasis and inline-code sentinels round-trip without exposing private-use input", () => {
  const rendered = renderSafeMarkdown("__bold__ _em_ `\uE000<b>x</b>` \uE001");
  assert.match(rendered, /<strong>bold<\/strong> <em>em<\/em>/);
  assert.match(rendered, /<code>&#57344;&lt;b&gt;x&lt;\/b&gt;<\/code>/);
  assert.match(rendered, /&#57345;/);
  assert.doesNotMatch(rendered, /[\uE000-\uF8FF]|<b>/);
});

test("plain escaping covers all attribute delimiters and private placeholder characters", () => {
  assert.equal(escapeHtml("&<>\"'"), "&amp;&lt;&gt;&quot;&#39;");
  assert.equal(escapeHtml("\uE000"), "&#57344;");
});

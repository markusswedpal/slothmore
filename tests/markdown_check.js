// Node checks for static/markdown.js. Run: node tests/markdown_check.js
"use strict";
const assert = require("assert");
const path = require("path");
const md = require(path.join(__dirname, "..", "static", "markdown.js"));
const R = (s, o) => md.renderMarkdown(s, o || { assetBase: "/asset?path=" });

function timed(label, fn, maxMs) {
  const t0 = Date.now();
  const out = fn();
  const ms = Date.now() - t0;
  assert.ok(ms < maxMs, label + " took " + ms + " ms (limit " + maxMs + ")");
  console.log("ok  " + label + " (" + ms + " ms)");
  return out;
}

// remote images are click-to-load placeholders; relative images go through the asset endpoint
let h = R("![logo](https://example.com/a.png) ![rel](img/b.png)");
assert.ok(h.includes('class="md-img-load"') && h.includes('data-md-img="https://example.com/a.png"'));
assert.ok(!/<img[^>]+example\.com/.test(h), "no remote <img> before a click");
assert.ok(h.includes('<img src="/asset?path=img%2Fb.png"'));
assert.ok(!R("![x](http://example.com/a.png)").includes("<img"), "plain http images are not loaded");
assert.ok(!R("![x](javascript:alert(1))").includes("javascript"));
console.log("ok  images");

// escaping and links
h = R("<script>alert(1)</script> [a](javascript:alert(1)) [b](https://x.dev)");
assert.ok(!h.includes("<script>") && !h.includes('href="javascript'));
assert.ok(h.includes('href="https://x.dev"'));
assert.ok(R("| a | b |\n|:---|---:|\n| 1 | 2 |").includes('class="ta-right"'), "no inline style in tables");
assert.ok(!R("| a |\n|:---:|\n| 1 |").includes("style="));
console.log("ok  escaping, links, tables");

// pathological inputs stay fast
timed("240 KB of '![a'", () => R("![a".repeat(80000)), 2000);
timed("240 KB of '[a'", () => R("[a".repeat(120000)), 2000);
timed("200 KB of '**'", () => R("**a".repeat(66000)), 2000);
timed("200 KB of backticks", () => R("`a``".repeat(50000)), 2000);
timed("200 KB of '<http://'", () => R("<http://a".repeat(22000)), 2000);

// nesting depth is capped
h = timed("5000 nested blockquotes", () => R(">".repeat(5000) + " deep"), 2000);
assert.ok((h.match(/<blockquote>/g) || []).length <= 8, "blockquote depth capped");
let list = "";
for (let d = 0; d < 200; d++) list += "  ".repeat(d) + "- item " + d + "\n";
h = timed("200-level nested list", () => R(list), 2000);
assert.ok((h.match(/<ul>/g) || []).length <= 8, "list depth capped");

// size cap with notice
h = R("line\n".repeat(60000));
assert.ok(h.includes("README truncated at 200 KB"), "truncation notice");
assert.ok(!R("short").includes("truncated"));
console.log("ok  size cap");

console.log("all markdown checks passed");

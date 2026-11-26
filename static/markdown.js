/* A small Markdown renderer for the in-app README viewer.
   No dependencies. Raw HTML is escaped. javascript:/data: URLs are dropped.
   Relative images are rewritten to a server endpoint; relative links are not navigable.
   Remote (https) images become click-to-load buttons. Inline parsing is linear in the input
   size; output is capped at 200 KB and nesting at 8 levels. */
(function (root) {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  }

  function safeHref(url) {
    var u = String(url || "").trim().replace(/\s+/g, "");
    if (!u) return null;
    if (u.charAt(0) === "#") return u;
    if (/^(https?:|mailto:)/i.test(u)) return u;
    return null;
  }

  function safeImg(url, assetBase) {
    var u = String(url || "").trim().replace(/\\/g, "/");
    if (!u || /\0/.test(u)) return null;
    if (/^(https?:)/i.test(u) && !/^https?:\/\//i.test(u.replace(/^\s+/, ""))) return null;
    if (/^(https?:)\/\//i.test(u)) return u;
    if (/^[a-z][a-z0-9+.-]*:/i.test(u) || u.indexOf("//") === 0 || u.charAt(0) === "/" || u.charAt(0) === "\\") return null;
    var parts = u.split("/").filter(function (p) { return p && p !== "."; });
    if (!parts.length || parts.some(function (p) { return p === ".."; })) return null;
    if (!assetBase) return null;
    return assetBase + encodeURIComponent(parts.join("/"));
  }

  var MAX_URL = 2048;

  // Finds the next index of `needle` at or after `from`, remembering results so repeated
  // lookups for unmatched markers stay linear instead of rescanning the rest of the text.
  function finder(s) {
    var memo = {};
    return function (needle, from) {
      var m = memo[needle];
      if (m && m.from <= from && (m.idx === -1 || m.idx >= from)) return m.idx;
      var idx = s.indexOf(needle, from);
      memo[needle] = { from: from, idx: idx };
      return idx;
    };
  }

  var URL_PART = /^\s*<?([^)\s>]+)>?(?:\s+"([^"]*)")?\s*$/;

  // [text](url "title") starting at s[open] === "[". Returns null or {text, url, title, end}.
  function bracketLink(s, open, next, minText) {
    var close = next("]", open + 1);
    if (close === -1 || close - open - 1 < minText || s.charAt(close + 1) !== "(") return null;
    var paren = next(")", close + 2);
    if (paren === -1 || paren - close - 2 > MAX_URL + 256) return null;
    var m = URL_PART.exec(s.slice(close + 2, paren));
    if (!m || m[1].length > MAX_URL) return null;
    return { text: s.slice(open + 1, close), url: m[1], title: m[2], end: paren + 1 };
  }

  var AUTOLINK = /<(https?:\/\/[^>\s]{1,2048})>/y;

  function inline(text, opts, depth) {
    depth = depth || 0;
    var s = String(text == null ? "" : text);
    if (depth > 6) return esc(s);
    var out = "";
    var i = 0;
    var next = finder(s);

    while (i < s.length) {
      var c = s.charAt(i);
      // backtick code: a run of N backticks closed by the next run of N backticks
      if (c === "`") {
        var r = i;
        while (s.charAt(r) === "`") r++;
        var run = s.slice(i, r);
        var endTick = next(run, r);
        if (endTick !== -1) {
          out += "<code>" + esc(s.slice(r, endTick)) + "</code>";
          i = endTick + run.length;
          continue;
        }
        out += esc(run);
        i = r;
        continue;
      }
      // image ![alt](url)
      if (c === "!" && s.charAt(i + 1) === "[") {
        var im = bracketLink(s, i + 1, next, 0);
        if (im) {
          out += imageHtml(im, opts);
          i = im.end;
          continue;
        }
      }
      // link [text](url)
      if (c === "[") {
        var lm = bracketLink(s, i, next, 1);
        if (lm) {
          var href = safeHref(lm.url);
          var inner = inline(lm.text, opts, depth + 1);
          if (href) {
            var ext = href.charAt(0) !== "#";
            out += '<a href="' + esc(href) + '"' +
              (ext ? ' target="_blank" rel="noopener noreferrer"' : "") +
              (lm.title ? ' title="' + esc(lm.title) + '"' : "") +
              ">" + inner + "</a>";
          } else {
            out += inner;
          }
          i = lm.end;
          continue;
        }
      }
      // strong ** ** or __ __
      if ((c === "*" || c === "_") && s.charAt(i + 1) === c) {
        var mark = c + c;
        var close = next(mark, i + 2);
        if (close !== -1) {
          out += "<strong>" + inline(s.slice(i + 2, close), opts, depth + 1) + "</strong>";
          i = close + 2;
          continue;
        }
      }
      // em * * or _ _ (not mid-word for _)
      if (c === "*" || (c === "_" && (i === 0 || /\s/.test(s.charAt(i - 1))))) {
        var j = next(c, i + 1);
        if (j !== -1 && j > i + 1) {
          out += "<em>" + inline(s.slice(i + 1, j), opts, depth + 1) + "</em>";
          i = j + 1;
          continue;
        }
      }
      // autolink <http://...>
      if (c === "<") {
        AUTOLINK.lastIndex = i;
        var am = AUTOLINK.exec(s);
        if (am && safeHref(am[1])) {
          out += '<a href="' + esc(am[1]) + '" target="_blank" rel="noopener noreferrer">' + esc(am[1]) + "</a>";
          i += am[0].length;
          continue;
        }
      }
      out += esc(c);
      i += 1;
    }
    return out;
  }

  function imageHtml(im, opts) {
    var src = safeImg(im.url, opts && opts.assetBase);
    var alt = im.text;
    if (!src) return esc(alt || im.url);
    if (/^https?:\/\//i.test(src)) {
      // Remote: a placeholder button. app.js swaps in the <img> only after a click.
      if (!/^https:\/\//i.test(src)) return esc(alt || src);
      var host = src.replace(/^https:\/\//i, "").split(/[\/?#]/)[0];
      return '<button type="button" class="md-img-load" data-md-img="' + esc(src) + '" data-alt="' + esc(alt) + '"' +
        (im.title ? ' data-title="' + esc(im.title) + '"' : "") +
        ' title="Load image from ' + esc(host) + '">Load image' + (alt ? ": " + esc(alt) : "") +
        " (" + esc(host) + ")</button>";
    }
    return '<img src="' + esc(src) + '" alt="' + esc(alt) + '"' +
      (im.title ? ' title="' + esc(im.title) + '"' : "") +
      ' loading="lazy" referrerpolicy="no-referrer">';
  }

  function isFence(line) {
    var m = /^(```+|~~~+)\s*([^\s]*)/.exec(line);
    return m ? { mark: m[1], lang: m[2] || "" } : null;
  }
  function isHeading(line) {
    var m = /^(#{1,6})\s+(.*)$/.exec(line);
    return m ? { level: m[1].length, text: m[2].replace(/\s+#+\s*$/, "") } : null;
  }
  function isHr(line) {
    return /^\s{0,3}((-\s*){3,}|(\*\s*){3,}|(_\s*){3,})\s*$/.test(line);
  }
  function isQuote(line) { return /^\s{0,3}>/.test(line); }
  function isUl(line) { return /^\s{0,3}[-*+]\s+/.test(line); }
  function isOl(line) { return /^\s{0,3}\d{1,9}[.)]\s+/.test(line); }
  function tableSplit(line) {
    var t = line.trim();
    if (t.charAt(0) === "|") t = t.slice(1);
    if (t.charAt(t.length - 1) === "|") t = t.slice(0, -1);
    return t.split("|").map(function (c) { return c.trim(); });
  }
  function isSep(line) {
    var cells = tableSplit(line);
    return cells.length > 0 && cells.every(function (c) { return /^:?-{3,}:?$/.test(c); });
  }
  function looksTable(lines, i) {
    return i + 1 < lines.length && /\|/.test(lines[i]) && isSep(lines[i + 1]);
  }

  var MAX_CHARS = 200 * 1024;   // rendered size cap; the rest is cut with a notice
  var MAX_DEPTH = 8;            // nested blockquotes / lists beyond this render as plain text

  function renderMarkdown(src, opts, depth) {
    opts = opts || {};
    depth = depth || 0;
    var text = String(src == null ? "" : src);
    var note = "";
    var cap = opts.maxChars || MAX_CHARS;
    if (depth === 0 && text.length > cap) {
      var cut = text.lastIndexOf("\n", cap);
      text = text.slice(0, cut > cap / 2 ? cut : cap);
      note = '<p class="md-note">README truncated at ' + Math.round(cap / 1024) +
        ' KB. Use \u201cView raw\u201d or open the file to read the rest.</p>';
    }
    var lines = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    var i = 0;
    var html = [];
    function nestedOrPlain(md) {
      if (depth + 1 >= MAX_DEPTH) return "<p>" + esc(md).replace(/\n/g, "<br>") + "</p>";
      return renderMarkdown(md, opts, depth + 1);
    }

    function takeFence(open) {
      i += 1;
      var buf = [];
      while (i < lines.length) {
        if (lines[i].slice(0, open.mark.length) === open.mark && /^\s*$/.test(lines[i].slice(open.mark.length))) break;
        buf.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      var cls = open.lang && /^[A-Za-z0-9_+-]+$/.test(open.lang) ? ' class="lang-' + esc(open.lang) + '"' : "";
      html.push("<pre><code" + cls + ">" + esc(buf.join("\n")) + "</code></pre>");
    }

    function takeQuote() {
      var buf = [];
      while (i < lines.length && isQuote(lines[i])) {
        buf.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
        i += 1;
      }
      html.push("<blockquote>" + nestedOrPlain(buf.join("\n")) + "</blockquote>");
    }

    function takeList() {
      var ordered = isOl(lines[i]);
      var items = [];
      while (i < lines.length && (ordered ? isOl(lines[i]) : isUl(lines[i]))) {
        var text = lines[i].replace(ordered ? /^\s{0,3}\d{1,9}[.)]\s+/ : /^\s{0,3}[-*+]\s+/, "");
        i += 1;
        var nested = [];
        while (i < lines.length && /^\s{2,}/.test(lines[i]) && lines[i].trim()) {
          nested.push(lines[i].replace(/^\s{2}/, "").replace(/^\s{2}/, ""));
          i += 1;
        }
        var body = inline(text, opts);
        if (nested.length) body += nestedOrPlain(nested.join("\n"));
        items.push("<li>" + body + "</li>");
      }
      html.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>"));
    }

    function takeTable() {
      var heads = tableSplit(lines[i]);
      var seps = tableSplit(lines[i + 1]);
      i += 2;
      var align = seps.map(function (c) {
        var left = c.charAt(0) === ":";
        var right = c.charAt(c.length - 1) === ":";
        return left && right ? "center" : right ? "right" : left ? "left" : "";
      });
      var rows = [];
      while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim() && !isHr(lines[i])) {
        rows.push(tableSplit(lines[i]));
        i += 1;
      }
      function cell(tag, val, idx) {
        var a = align[idx] ? ' class="ta-' + align[idx] + '"' : "";
        return "<" + tag + a + ">" + inline(val, opts) + "</" + tag + ">";
      }
      var thead = "<thead><tr>" + heads.map(function (h, n) { return cell("th", h, n); }).join("") + "</tr></thead>";
      var tbody = "<tbody>" + rows.map(function (r) {
        return "<tr>" + heads.map(function (_, n) { return cell("td", r[n] == null ? "" : r[n], n); }).join("") + "</tr>";
      }).join("") + "</tbody>";
      html.push("<table>" + thead + tbody + "</table>");
    }

    function takePara() {
      var buf = [];
      while (i < lines.length) {
        var line = lines[i];
        if (!line.trim()) break;
        if (isFence(line) || isHeading(line) || isHr(line) || isQuote(line) || isUl(line) || isOl(line) || looksTable(lines, i)) break;
        buf.push(line);
        i += 1;
      }
      var joined = buf.join("\n").replace(/  \n/g, "<br>\n");
      html.push("<p>" + inline(joined, opts) + "</p>");
    }

    while (i < lines.length) {
      var line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      var fence = isFence(line);
      if (fence) { takeFence(fence); continue; }
      var h = isHeading(line);
      if (h) { html.push("<h" + h.level + ">" + inline(h.text, opts) + "</h" + h.level + ">"); i += 1; continue; }
      if (isHr(line)) { html.push("<hr>"); i += 1; continue; }
      if (isQuote(line)) { takeQuote(); continue; }
      if (isUl(line) || isOl(line)) { takeList(); continue; }
      if (looksTable(lines, i)) { takeTable(); continue; }
      takePara();
    }
    return html.join("") + note;
  }

  var api = { renderMarkdown: renderMarkdown, safeHref: safeHref, safeImg: safeImg, esc: esc };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.renderMarkdown = renderMarkdown;
  root.SlothmoreMarkdown = api;
})(typeof window !== "undefined" ? window : typeof globalThis !== "undefined" ? globalThis : this);

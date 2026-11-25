/* A small Markdown renderer for the in-app README viewer.
   No dependencies. Raw HTML is escaped. javascript:/data: URLs are dropped.
   Relative images are rewritten to a server endpoint; relative links are not navigable. */
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

  function inline(text, opts) {
    var out = "";
    var i = 0;
    var s = String(text == null ? "" : text);
    function rest() { return s.slice(i); }

    while (i < s.length) {
      // backtick code (one or more backticks)
      if (s.charAt(i) === "`") {
        var m = rest().match(/^(`+)([^`]|[\s\S]*?)\1/);
        if (m) {
          out += "<code>" + esc(m[2]) + "</code>";
          i += m[0].length;
          continue;
        }
      }
      // image ![alt](url)
      if (s.charAt(i) === "!" && s.charAt(i + 1) === "[") {
        var im = rest().match(/^!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"([^"]*)")?\s*\)/);
        if (im) {
          var src = safeImg(im[2], opts && opts.assetBase);
          if (src) {
            out += '<img src="' + esc(src) + '" alt="' + esc(im[1]) + '"' +
              (im[3] ? ' title="' + esc(im[3]) + '"' : "") +
              ' loading="lazy" referrerpolicy="no-referrer">';
          } else {
            out += esc(im[1] || im[2]);
          }
          i += im[0].length;
          continue;
        }
      }
      // link [text](url)
      if (s.charAt(i) === "[") {
        var lm = rest().match(/^\[([^\]]+)\]\(\s*<?([^)\s>]+)>?(?:\s+"([^"]*)")?\s*\)/);
        if (lm) {
          var href = safeHref(lm[2]);
          var inner = inline(lm[1], opts);
          if (href) {
            var ext = href.charAt(0) !== "#";
            out += '<a href="' + esc(href) + '"' +
              (ext ? ' target="_blank" rel="noopener noreferrer"' : "") +
              (lm[3] ? ' title="' + esc(lm[3]) + '"' : "") +
              ">" + inner + "</a>";
          } else {
            out += inner;
          }
          i += lm[0].length;
          continue;
        }
      }
      // strong ** ** or __ __
      if ((s.charAt(i) === "*" && s.charAt(i + 1) === "*") || (s.charAt(i) === "_" && s.charAt(i + 1) === "_")) {
        var mark = s.slice(i, i + 2);
        var close = s.indexOf(mark, i + 2);
        if (close !== -1) {
          out += "<strong>" + inline(s.slice(i + 2, close), opts) + "</strong>";
          i = close + 2;
          continue;
        }
      }
      // em * * or _ _ (not mid-word for _)
      if (s.charAt(i) === "*" || (s.charAt(i) === "_" && (i === 0 || /\s/.test(s.charAt(i - 1))))) {
        var ch = s.charAt(i);
        var j = s.indexOf(ch, i + 1);
        if (j !== -1 && j > i + 1) {
          out += "<em>" + inline(s.slice(i + 1, j), opts) + "</em>";
          i = j + 1;
          continue;
        }
      }
      // autolink <http://...>
      if (s.charAt(i) === "<") {
        var am = rest().match(/^<(https?:\/\/[^>\s]+)>/);
        if (am && safeHref(am[1])) {
          out += '<a href="' + esc(am[1]) + '" target="_blank" rel="noopener noreferrer">' + esc(am[1]) + "</a>";
          i += am[0].length;
          continue;
        }
      }
      out += esc(s.charAt(i));
      i += 1;
    }
    return out;
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

  function renderMarkdown(src, opts) {
    opts = opts || {};
    var lines = String(src == null ? "" : src).replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    var i = 0;
    var html = [];

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
      html.push("<blockquote>" + renderMarkdown(buf.join("\n"), opts) + "</blockquote>");
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
        if (nested.length) body += renderMarkdown(nested.join("\n"), opts);
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
        var a = align[idx] ? ' style="text-align:' + align[idx] + '"' : "";
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
    return html.join("");
  }

  var api = { renderMarkdown: renderMarkdown, safeHref: safeHref, safeImg: safeImg, esc: esc };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.renderMarkdown = renderMarkdown;
  root.LauncherMarkdown = api;
})(typeof window !== "undefined" ? window : typeof globalThis !== "undefined" ? globalThis : this);

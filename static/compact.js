"use strict";
// Slothmore quick panel: the compact view shown under the menu bar icon (also works in a normal browser tab at /compact).
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const NATIVE = (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.slothmore) || null;
const Q = { items: [], ports: [], ai: { servers: [], agents: [] }, q: "", rows: [], sel: 0, open: {}, confirm: null, busy: false, timer: null, statusTimer: null };

// Messages to the menu bar helper (WKScriptMessageHandler "slothmore"). In a browser tab we fall back to plain web behaviour.
function native(action, extra) {
  const msg = Object.assign({ action }, extra || {});
  if (NATIVE) { try { NATIVE.postMessage(msg); return true; } catch (e) {} }
  return false;
}
function openUrl(url) { if (!url) return; if (!native("openURL", { url })) window.open(url, "_blank", "noopener"); }
function openFull() { if (!native("openFull")) window.open("/", "_blank", "noopener"); }
function closePanel() { native("close"); }
function quitHelper() { if (!native("quit")) status("Quit closes the menu bar helper; this is a browser tab.", "info"); }

async function api(path, method, body) {
  const opt = { method: method || "GET", headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let res, data = {};
  try { res = await fetch(path, opt); } catch (e) { return { ok: false, status: 0, data: { error: "Slothmore isn't running." } }; }
  try { data = await res.json(); } catch (e) {}
  return { ok: res.ok, status: res.status, data };
}

function status(text, kind) {
  const el = $("#status");
  clearTimeout(Q.statusTimer);
  if (!text) { el.hidden = true; fit(); return; }
  el.textContent = text;
  el.className = "qp-status " + (kind || "");
  el.hidden = false;
  fit();
  if (kind !== "err") Q.statusTimer = setTimeout(() => status(""), 3500);
}

// ------------------------------------------------------------------ data
async function refresh() {
  const [it, po, ai] = await Promise.all([api("/api/items"), api("/api/ports"), api("/api/ai")]);
  if (it.status === 401) { status("Signed out. Run `slothmore open` in Terminal, then reopen the panel.", "err"); return; }
  if (!it.ok) { status(it.data.error || "Can't load projects.", "err"); return; }
  Q.items = it.data.items || [];
  Q.ports = po.ok ? (po.data.ports || []) : [];
  Q.ai = ai.ok ? { servers: ai.data.servers || [], agents: ai.data.agents || [] } : { servers: [], agents: [] };
  render();
}

// ------------------------------------------------------------------ search + model
// Score a candidate against the query: prefix > word start > substring > in-order letters. 0 = no match.
function score(q, text) {
  if (!q) return 1;
  const t = String(text || "").toLowerCase();
  const i = t.indexOf(q);
  if (i === 0) return 100;
  if (i > 0) return /[\s\-_./:]/.test(t[i - 1]) ? 80 : 60;
  let k = 0;
  for (const ch of t) if (ch === q[k]) k++;
  return k === q.length ? 20 : 0;
}
function best(q, ...fields) { return Math.max(0, ...fields.map((f) => score(q, f))); }
function host(url) { try { return new URL(url).host; } catch (e) { return url || ""; } }
function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 129600) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}

function portGroups() {
  const by = new Map();
  for (const p of Q.ports) {
    const key = p.pid ? "pid:" + p.pid : "port:" + p.port;
    if (!by.has(key)) by.set(key, { key, pid: p.pid, command: p.command, user: p.user, ours: false, can_stop: false, item_name: p.item_name, ports: [] });
    const g = by.get(key);
    g.ports.push(p);
    g.ours = g.ours || p.ours;
    g.can_stop = g.can_stop || p.can_stop;
    g.item_name = g.item_name || p.item_name;
  }
  const out = [...by.values()];
  out.forEach((g) => g.ports.sort((a, b) => a.port - b.port));
  // Ports of Slothmore projects first, then ones you can stop, then by lowest port.
  out.sort((a, b) => (!!b.item_name - !!a.item_name) || (b.can_stop - a.can_stop) || (a.ports[0].port - b.ports[0].port));
  return out;
}

function buildRows() {
  const q = Q.q.trim().toLowerCase();
  const sections = [];
  const running = Q.items.filter((i) => i.running);
  const runRows = running.map((i) => ({ kind: "run", id: i.id, s: best(q, i.name, i.folder, i.url && host(i.url)), it: i }))
    .filter((r) => r.s > 0).sort((a, b) => b.s - a.s || a.it.name.localeCompare(b.it.name));
  sections.push({ title: "Running now", rows: runRows, empty: q ? "" : "Nothing running." });

  const lastStart = (i) => (i.last_run && (i.last_run.started || i.last_run.ended)) || 0;
  const projects = Q.items.filter((i) => !i.running && i.startable)
    .map((i) => ({ kind: "proj", id: i.id, s: best(q, i.name, i.folder, i.lang), it: i }))
    .filter((r) => r.s > 0)
    .sort((a, b) => (b.s - a.s) || ((b.it.source === "pinned") - (a.it.source === "pinned")) || (lastStart(b.it) - lastStart(a.it)) || a.it.name.localeCompare(b.it.name));
  sections.push({ title: "Quick start", rows: projects.slice(0, q ? 30 : 5), more: Math.max(0, projects.length - (q ? 30 : 5)), empty: q ? "" : "No startable projects." });

  const ports = portGroups().map((g) => ({ kind: "port", id: g.key, s: best(q, g.command, g.item_name, ...g.ports.map((p) => ":" + p.port), String(g.pid || "")), g }))
    .filter((r) => r.s > 0).sort((a, b) => b.s - a.s);
  sections.push({ title: "Ports", rows: ports.slice(0, q ? 30 : 5), more: Math.max(0, ports.length - (q ? 30 : 5)), empty: q ? "" : "No listening ports." });

  const ai = Q.ai.servers.filter((s) => s.running).map((s) => ({ kind: "ai", id: s.id, s: best(q, s.name, ":" + s.port, ...(s.models || []).map((m) => m.name)), srv: s }));
  const agentsBy = new Map();
  for (const a of Q.ai.agents) { const k = a.name || a.key; agentsBy.set(k, (agentsBy.get(k) || []).concat([a])); }
  for (const [name, list] of agentsBy) ai.push({ kind: "agent", id: "agent:" + name, s: best(q, name, ...list.map((a) => a.item_name || a.cwd || "")), name, list });
  // Without a query the AI summary lives in the strip above the footer; searching lists the matches as rows.
  if (q) sections.push({ title: "AI models & agents", rows: ai.filter((r) => r.s > 0), empty: "" });
  return sections;
}

// ------------------------------------------------------------------ render
const ICON_OPEN = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>';

function rowHtml(r, idx) {
  const sel = idx === Q.sel ? " sel" : "";
  const a = (act, label, cls, title) => '<button type="button" class="qb ' + (cls || "") + '" data-act="' + act + '" data-i="' + idx + '"' + (title ? ' title="' + esc(title) + '"' : "") + ">" + label + "</button>";
  if (r.kind === "run") {
    const i = r.it, sub = [i.url ? host(i.url) : (i.port ? ":" + i.port : ""), i.started_at ? "up " + ago(i.started_at).replace(" ago", "") : "", i.prompt ? "waiting for input" : ""].filter(Boolean).join(" · ");
    return '<div class="row' + sel + '" role="option" data-i="' + idx + '"><span class="dot on"></span><span class="r-main"><span class="r-name">' + esc(i.name) + '</span><span class="r-sub">' + esc(sub || i.run_cmd || "") + '</span></span><span class="r-acts">' +
      (i.url ? a("open", ICON_OPEN, "", "Open " + i.url) : "") + a("restart", "↻", "", "Restart") + a("stop", "Stop", "danger") + "</span></div>";
  }
  if (r.kind === "proj") {
    const i = r.it, sub = [i.source === "pinned" ? "pinned" : "", i.last_run ? "ran " + ago(i.last_run.started) : "", i.run_cmd || i.cmd || ""].filter(Boolean).join(" · ");
    return '<div class="row' + sel + '" role="option" data-i="' + idx + '"><span class="dot"></span><span class="r-main"><span class="r-name">' + esc(i.name) + '</span><span class="r-sub">' + esc(sub) + '</span></span><span class="r-acts">' + a("start", "Start", "primary") + "</span></div>";
  }
  if (r.kind === "port") {
    const g = r.g, n = g.ports.length, open = !!Q.open[g.key];
    const name = g.item_name || g.command || "unknown process";
    const sub = [g.item_name && g.command ? g.command : "", "PID " + (g.pid || "?"), n === 1 ? ":" + g.ports[0].port : n + " ports", g.can_stop ? "" : (g.ours ? "this Slothmore" : (g.user ? g.user : ""))].filter(Boolean).join(" · ");
    let h = '<div class="row' + sel + '" role="option" data-i="' + idx + '"><span class="chev" data-act="toggle" data-i="' + idx + '">' + (n > 1 ? (open ? "▾" : "▸") : "") + '</span><span class="r-main"><span class="r-name">' + esc(name) + (g.ours ? '<span class="badge">this</span>' : "") + '</span><span class="r-sub">' + esc(sub) + '</span></span><span class="r-acts">' +
      (n === 1 && g.ports[0].url ? a("open", ICON_OPEN, "", "Open " + g.ports[0].url) : "") + (g.can_stop ? a("stop", "Stop", "danger") : "") + "</span></div>";
    if (open && n > 1) h += '<div class="sub-ports">' + g.ports.map((p) => '<button type="button" class="pchip" data-act="openport" data-i="' + idx + '" data-port="' + p.port + '"' + (p.url ? "" : " disabled") + ">:" + p.port + (p.address && p.address !== "127.0.0.1" ? " " + esc(p.address) : "") + "</button>").join("") + "</div>";
    return h;
  }
  if (r.kind === "ai") {
    const s = r.srv, loaded = (s.models || []).filter((m) => m.loaded).map((m) => m.name);
    const sub = [":" + s.port, (s.models || []).length + " models", loaded.length ? "loaded: " + loaded.slice(0, 2).join(", ") : "", s.memory_label || ""].filter(Boolean).join(" · ");
    return '<div class="row' + sel + '" role="option" data-i="' + idx + '"><span class="dot ai"></span><span class="r-main"><span class="r-name">' + esc(s.name) + '</span><span class="r-sub">' + esc(sub) + '</span></span><span class="r-acts">' + a("open", ICON_OPEN, "", "Open " + s.url) + "</span></div>";
  }
  const where = [...new Set(r.list.map((x) => x.item_name || (x.cwd || "").split("/").pop()).filter(Boolean))];
  const sub = r.list.length + (r.list.length === 1 ? " session" : " sessions") + (where.length ? " · " + where.slice(0, 3).join(", ") : "");
  return '<div class="row' + sel + '" role="option" data-i="' + idx + '"><span class="dot ai"></span><span class="r-main"><span class="r-name">' + esc(r.name) + '</span><span class="r-sub">' + esc(sub) + "</span></span></div>";
}

function renderAi() {
  const el = $("#aiStrip");
  const servers = Q.ai.servers.filter((s) => s.running);
  const agents = Q.ai.agents;
  if (!servers.length && !agents.length) { el.hidden = true; return; }
  let h = '<span class="lbl">AI</span>';
  for (const s of servers) {
    const loaded = (s.models || []).filter((m) => m.loaded).map((m) => m.name);
    h += '<button type="button" class="aichip" data-url="' + esc(s.url) + '" title="Open ' + esc(s.url) + '"><span class="dot ai"></span>' + esc(s.name) + ' <span class="m">:' + s.port + " · " +
      (loaded.length ? esc(loaded[0]) + (loaded.length > 1 ? " +" + (loaded.length - 1) : "") + " loaded" : (s.models || []).length + " models") + "</span></button>";
  }
  if (agents.length) {
    const names = [...new Set(agents.map((a) => a.name || a.key))];
    h += '<span class="aichip" title="' + esc(names.join(", ")) + '"><span class="dot ai"></span>' + agents.length + (agents.length === 1 ? " agent" : " agents") + ' <span class="m">' + esc(names.slice(0, 2).join(", ")) + "</span></span>";
  }
  el.innerHTML = h;
  el.hidden = false;
}

function render() {
  renderAi();
  const sections = buildRows();
  Q.rows = [];
  let h = "";
  for (const sec of sections) {
    if (!sec.rows.length && !sec.empty) continue;
    h += '<div class="qp-sec">' + esc(sec.title) + (sec.rows.length ? ' <span class="n">' + sec.rows.length + (sec.more ? "+" : "") + "</span>" : "") + "</div>";
    if (!sec.rows.length) { h += '<div class="empty">' + esc(sec.empty) + "</div>"; continue; }
    for (const r of sec.rows) { h += rowHtml(r, Q.rows.length); Q.rows.push(r); }
    if (sec.more) h += '<div class="empty">+' + sec.more + " more · type to search</div>";
  }
  if (!Q.rows.length && Q.q) h = '<div class="empty">No match for “' + esc(Q.q) + '”.</div>';
  if (Q.sel >= Q.rows.length) Q.sel = Math.max(0, Q.rows.length - 1);
  $("#list").innerHTML = h;
  // Re-apply selection (rowHtml used Q.sel before clamping).
  document.querySelectorAll(".row").forEach((el) => el.classList.toggle("sel", Number(el.dataset.i) === Q.sel));
  fit();
}

// Tell the panel how tall we are (auto-height, capped at 600 by CSS and by the helper).
let lastH = 0;
function fit() {
  requestAnimationFrame(() => {
    const h = Math.min(600, Math.ceil($("#qp").scrollHeight));
    if (h !== lastH) { lastH = h; native("height", { value: h }); }
  });
}

function select(i) {
  if (!Q.rows.length) return;
  Q.sel = (i + Q.rows.length) % Q.rows.length;
  document.querySelectorAll(".row").forEach((el) => el.classList.toggle("sel", Number(el.dataset.i) === Q.sel));
  const el = document.querySelector('.row[data-i="' + Q.sel + '"]');
  if (el) el.scrollIntoView({ block: "nearest" });
}

// ------------------------------------------------------------------ actions
function ask(text, yesLabel, fn) {
  Q.confirm = fn;
  $("#confirmText").textContent = text;
  $("#confirmYes").textContent = yesLabel || "Stop";
  $("#confirm").hidden = false;
  $("#confirmYes").focus();
  fit();
}
function closeAsk() { Q.confirm = null; $("#confirm").hidden = true; $("#q").focus(); fit(); }

async function itemAction(it, action) {
  status((action === "start" ? "Starting " : action === "stop" ? "Stopping " : "Restarting ") + it.name + "…");
  const r = await api("/api/items/" + encodeURIComponent(it.id) + "/" + action, "POST", {});
  if (!r.ok) status(r.data.detail || r.data.error || ("Couldn't " + action + " " + it.name + "."), "err");
  else status(it.name + " " + ({ start: "started", stop: "stopped", restart: "restarted" }[action]) + ".", "ok");
  setTimeout(refresh, 400);
}

async function stopPort(g) {
  const p = g.ports.find((x) => x.can_stop) || g.ports[0];
  const r = await api("/api/ports/stop", "POST", { port: p.port, pid: g.pid, confirm: true });
  if (!r.ok) status(r.data.detail || r.data.error || "Couldn't stop that process.", "err");
  else status("Stopped " + (g.command || "PID " + g.pid) + ".", "ok");
  setTimeout(refresh, 500);
}

function primary(r) {
  if (!r) return;
  if (r.kind === "run") return r.it.url ? openUrl(r.it.url) : status(r.it.name + " has no web page.");
  if (r.kind === "proj") return itemAction(r.it, "start");
  if (r.kind === "port") {
    if (r.g.ports.length > 1) { Q.open[r.g.key] = !Q.open[r.g.key]; return render(); }
    return r.g.ports[0].url ? openUrl(r.g.ports[0].url) : status("Port :" + r.g.ports[0].port + " isn't a web page.");
  }
  if (r.kind === "ai") return openUrl(r.srv.url);
}

function stopRow(r) {
  if (!r) return;
  if (r.kind === "run") return ask("Stop " + r.it.name + "?", "Stop", () => itemAction(r.it, "stop"));
  if (r.kind === "port" && r.g.can_stop) {
    const ports = r.g.ports.map((p) => ":" + p.port).join(", ");
    return ask("Stop " + (r.g.command || "process") + " (PID " + r.g.pid + ", " + ports + ")? It gets SIGTERM.", "Stop", () => stopPort(r.g));
  }
}

function act(name, idx, el) {
  const r = Q.rows[idx];
  if (!r) return;
  select(idx);
  if (name === "open") return primary(r.kind === "port" ? { kind: "port", g: Object.assign({}, r.g, { ports: [r.g.ports[0]] }) } : r);
  if (name === "start" || name === "restart") return itemAction(r.it, name);
  if (name === "stop") return stopRow(r);
  if (name === "toggle") { Q.open[r.g.key] = !Q.open[r.g.key]; return render(); }
  if (name === "openport") { const p = r.g.ports.find((x) => String(x.port) === el.dataset.port); if (p && p.url) openUrl(p.url); }
}

function killAll() {
  const n = Q.items.filter((i) => i.running).length;
  if (!n) return status("Nothing Slothmore started is running.");
  ask("Stop all " + n + " item" + (n === 1 ? "" : "s") + " Slothmore started? Other processes are left alone.", "Kill all mine", async () => {
    const r = await api("/api/runs/stop-all", "POST", { confirm: true });
    if (!r.ok) status(r.data.detail || r.data.error || "Couldn't stop everything.", "err");
    else status("Stopped " + r.data.stopped + " item" + (r.data.stopped === 1 ? "" : "s") + ".", "ok");
    setTimeout(refresh, 600);
  });
}

// ------------------------------------------------------------------ events
$("#q").addEventListener("input", (e) => { Q.q = e.target.value; Q.sel = 0; render(); });
$("#list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]");
  if (b) { e.stopPropagation(); return act(b.dataset.act, Number(b.dataset.i), b); }
  const row = e.target.closest(".row");
  if (row) select(Number(row.dataset.i));
});
$("#list").addEventListener("dblclick", (e) => { const row = e.target.closest(".row"); if (row && !e.target.closest("[data-act]")) primary(Q.rows[Number(row.dataset.i)]); });
$("#aiStrip").addEventListener("click", (e) => { const b = e.target.closest("[data-url]"); if (b) openUrl(b.dataset.url); });
$("#confirmNo").addEventListener("click", closeAsk);
$("#confirmYes").addEventListener("click", () => { const fn = Q.confirm; closeAsk(); if (fn) fn(); });
$("#refreshBtn").addEventListener("click", () => { status("Refreshing…"); refresh(); });
$("#fullBtn").addEventListener("click", openFull);
$("#killBtn").addEventListener("click", killAll);
$("#quitBtn").addEventListener("click", quitHelper);

document.addEventListener("keydown", (e) => {
  const mod = IS_MAC ? e.metaKey : e.ctrlKey;
  const k = e.key.toLowerCase();
  if (mod && k === "r") { e.preventDefault(); status("Refreshing…"); return refresh(); }
  if (mod && k === "o") { e.preventDefault(); return openFull(); }
  if (mod && k === "q") { e.preventDefault(); return quitHelper(); }
  if (mod && e.key === "Backspace") { e.preventDefault(); return stopRow(Q.rows[Q.sel]); }
  if (e.key === "Escape") {
    e.preventDefault();
    if (Q.confirm) return closeAsk();
    return closePanel();
  }
  if (Q.confirm) return;  // the confirm box owns Enter/Tab while open
  if (e.key === "ArrowDown") { e.preventDefault(); return select(Q.sel + 1); }
  if (e.key === "ArrowUp") { e.preventDefault(); return select(Q.sel - 1); }
  if (e.key === "Enter") { e.preventDefault(); return primary(Q.rows[Q.sel]); }
  if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && Q.rows[Q.sel] && Q.rows[Q.sel].kind === "port" && e.target.id === "q" && !Q.q) {
    Q.open[Q.rows[Q.sel].g.key] = e.key === "ArrowRight"; return render();
  }
});

// Called by the menu bar helper each time the panel is shown.
window.slothmoreShown = function () { $("#q").focus(); $("#q").select(); refresh(); };
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden && !Q.confirm) refresh(); }, 4000);
$("#q").focus();
refresh();

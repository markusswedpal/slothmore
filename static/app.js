"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const YN = /[\[(]\s*(?:[yY](?:es)?\s*[\/|]\s*[nN](?:o)?|[nN](?:o)?\s*[\/|]\s*[yY](?:es)?)\s*[\])]/;
const FOLDER = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="flex:0 0 18px"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const PENCIL = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/></svg>';
const PLAY = '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 4l14 8-14 8z"/></svg>';
const STOP = '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>';
const OPEN = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>';

const RESTART_ICO = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>';
const S = { items: [], watch: [], pinned: 0, port: null, kind: "all", source: "all", q: "", sel: null,
            es: null, esId: null, esSeq: 0, last: "", dragId: null, argsDraft: {}, pendingRender: false,
            presetOpen: {}, envOpen: {}, envReveal: {}, envDraft: {}, envValues: {}, palIdx: 0, palItems: [],
            groups: [], settings: null, evSeq: -1, past: null, filter: "", histId: null, editGroup: null, groupOrder: [] };

// ---------------------------------------------------------------- api
async function api(path, method, body) {
  const opt = { method: method || "GET", headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opt); } catch (e) { return { ok: false, status: 0, data: { error: "Can't reach the Slothmore server." } }; }
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (res.status === 401) showSignedOut(); else if (res.ok) hideSignedOut();
  return { ok: res.ok, status: res.status, data };
}

// Signed out: the access cookie is missing or the token was reset (`slothmore --reset-token`).
function showSignedOut() {
  let b = document.getElementById("authBanner");
  if (!b) {
    b = document.createElement("div");
    b.id = "authBanner";
    b.className = "auth-banner";
    b.setAttribute("role", "alert");
    b.innerHTML = "<b>Signed out.</b> Run <code>slothmore open</code> in Terminal to open Slothmore signed in.";
    document.body.appendChild(b);
  }
  b.hidden = false;
}
function hideSignedOut() {
  const b = document.getElementById("authBanner");
  if (b) b.hidden = true;
}

function applyListing(d) {
  if (!d || !d.items) return;
  // CPU/memory change every sample; patch those in place instead of re-rendering every card.
  const json = JSON.stringify(d, (k, v) => (k === "health" ? undefined : v));
  S.items = d.items;
  if (json === S.last) { updateHealth(); return; }
  S.last = json;
  S.watch = d.watch; S.pinned = d.pinned; S.port = d.port; S.groups = d.groups || [];
  render();
  if (typeof palDlg !== "undefined" && palDlg.open) renderPalette();
}
async function refresh() {
  if (S.dragId) return;
  const r = await api("/api/items");
  if (r.ok) applyListing(r.data);
  syncStream();
  renderTermHeader();
  pollEvents();
}

// ---------------------------------------------------------------- health helpers
function fmtBytes(n) {
  if (n == null) return "–";
  if (n < 1024 * 1024) return Math.round(n / 1024) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}
function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec));
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m " + String(sec % 60).padStart(2, "0") + "s";
  if (sec < 86400) return Math.floor(sec / 3600) + "h " + String(Math.floor(sec % 3600 / 60)).padStart(2, "0") + "m";
  return Math.floor(sec / 86400) + "d " + Math.floor(sec % 86400 / 3600) + "h";
}
function fmtAgo(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + " min ago";
  if (d < 86400) return Math.floor(d / 3600) + " h ago";
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function fmtWhen(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function healthInner(it, short) {
  const h = it.health;
  const up = it.started_at ? '<span data-since="' + it.started_at + '">' + fmtDur(Date.now() / 1000 - it.started_at) + "</span>" : "–";
  if (!h) return short ? "up " + up : '<span>CPU <b>…</b></span><span>MEM <b>…</b></span><span>UP <b>' + up + "</b></span>";
  if (short) return h.cpu.toFixed(1) + "% · " + fmtBytes(h.rss) + " · up " + up;
  return '<span>CPU <b>' + h.cpu.toFixed(1) + '%</b></span><span>MEM <b>' + fmtBytes(h.rss) + '</b></span><span>UP <b>' + up + "</b></span>" +
    (h.procs > 1 ? "<span>" + h.procs + " procs</span>" : "");
}
function updateHealth() {
  document.querySelectorAll("[data-health-for]").forEach((el) => {
    const it = S.items.find((x) => x.id === el.dataset.healthFor);
    if (it && it.running) el.innerHTML = healthInner(it, el.dataset.short === "1");
  });
}
setInterval(() => {
  const now = Date.now() / 1000;
  document.querySelectorAll("[data-since]").forEach((el) => { el.textContent = fmtDur(now - Number(el.dataset.since)); });
}, 1000);

// ---------------------------------------------------------------- notifications (browser + in-page)
function browserNotifyOK() {
  return typeof Notification !== "undefined" && Notification.permission === "granted";
}
let evBusy = false;
async function pollEvents() {
  if (evBusy) return;            // overlapping refreshes must not deliver the same event twice
  evBusy = true;
  try {
    const ns = S.settings && S.settings.notify;
    const browser = ns && ns.enabled && ns.browser && browserNotifyOK() ? 1 : 0;
    const r = await api("/api/events?after=" + S.evSeq + "&browser=" + browser);
    if (!r.ok) return;
    const first = S.evSeq < 0, prev = S.evSeq;
    S.evSeq = Math.max(S.evSeq, r.data.seq);
    if (first) return;
    (r.data.events || []).filter((ev) => ev.seq > prev).forEach(showEvent);
  } finally { evBusy = false; }
}
function showEvent(ev) {
  const ns = (S.settings && S.settings.notify) || {};
  const visible = document.visibilityState === "visible";
  if (visible || ev.kind === "test") toast(ev);
  if ((!visible || ev.kind === "test") && ns.browser && browserNotifyOK()) {
    try {
      const n = new Notification(ev.title, { body: ev.body, tag: "launcher-" + (ev.id || "test") + "-" + ev.kind, icon: "/icons/icon-192.png" });
      n.onclick = () => { window.focus(); if (ev.id) select(ev.id); n.close(); };
    } catch (e) {}
  }
}
function toast(ev) {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = "toast " + ev.kind;
  el.setAttribute("role", "status");
  el.innerHTML = "<b>" + esc(ev.title) + "</b><span>" + esc(ev.body) + "</span>";
  el.onclick = () => { if (ev.id) select(ev.id); el.remove(); };
  box.appendChild(el);
  while (box.children.length > 4) box.firstChild.remove();
  setTimeout(() => el.remove(), 8000);
}
async function loadSettings() {
  const r = await api("/api/settings");
  if (r.ok) { S.settings = r.data; renderNotifBtn(); S.last = ""; render(); }
}
function renderNotifBtn() {
  const ns = S.settings && S.settings.notify;
  $("#notifBtn").textContent = "Notifications: " + (ns && ns.enabled ? "on" : "off");
}

// ---------------------------------------------------------------- notices
function notify(msg, actions) {
  $("#noticeMsg").textContent = msg;
  const box = $("#noticeActions"); box.innerHTML = "";
  (actions || []).forEach((a) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "btn"; b.textContent = a.label; b.onclick = () => { a.run(); hideNotice(); };
    box.appendChild(b);
  });
  $("#notice").classList.add("show");
}
function hideNotice() { $("#notice").classList.remove("show"); }
$("#noticeClose").onclick = hideNotice;

// ---------------------------------------------------------------- rendering
function statusOf(it) {
  if (it.running) {
    if (it.prompt) return { cls: "wait", label: "Waiting for input" };
    return { cls: "run", label: "Running" + (it.port ? " · :" + it.port : "") };
  }
  if (it.auto_restart_status) {
    return { cls: "wait", label: "Auto-restart: " + it.auto_restart_status };
  }
  if (it.has_run && it.exit_code !== null && it.exit_code !== undefined) {
    if (it.exit_code === 0) return { cls: "", label: "Finished" };
    if (it.exit_code < 0) return { cls: "", label: "Stopped" };
    return { cls: "", label: "Exited (code " + it.exit_code + ")" };
  }
  return { cls: "", label: "Stopped" };
}

function matchesKind(it) {
  switch (S.kind) {
    case "web": return it.web;
    case "python": return it.lang === "python";
    case "node": return it.lang === "node";
    case "scripts": return !it.web && it.startable;
    case "ai": return !!it.ai || !!(it.agents && it.agents.length);
    default: return true;
  }
}
function visible(it) {
  if (!matchesKind(it)) return false;
  if (S.q) { const q = S.q.toLowerCase(); if (![it.name, it.folder, it.base_name, it.rel, it.path].some((v) => v && String(v).toLowerCase().includes(q))) return false; }
  return true;
}

function getArgs(it) {
  return S.argsDraft[it.id] !== undefined ? S.argsDraft[it.id] : (it.args || "");
}
function envToText(vars, values) {
  values = values || {};
  return (vars || []).map((v) => v.key + "=" + (values[v.key] != null ? values[v.key] : "")).join("\n");
}
// Env values are not part of /api/items; Reveal fetches them from /api/items/<id>/env.
async function loadEnvValues(id) {
  const r = await api("/api/items/" + encodeURIComponent(id) + "/env");
  if (!r.ok || !r.data || typeof r.data.env !== "object") { notify("Could not load the env values."); return false; }
  S.envValues[id] = r.data.env;
  return true;
}
function isEditingSections() {
  const ae = document.activeElement;
  const sec = $("#sections");
  return !!(ae && sec && sec.contains(ae) && (ae.tagName === "INPUT" || ae.tagName === "SELECT" || ae.tagName === "TEXTAREA"));
}

function subLine(it) {
  // Folder name stays visible under a custom name; nested items show their path inside the watched folder.
  const rel = it.rel && it.rel.indexOf("/") !== -1 ? it.rel : null;
  const folder = it.folder || it.base_name || it.name;
  if (rel) {
    const cut = rel.lastIndexOf("/");
    return '<span class="rel-dim">' + esc(rel.slice(0, cut + 1)) + "</span>" + esc(rel.slice(cut + 1));
  }
  if (it.custom_name || (folder && folder !== it.name)) return esc(folder);
  return "";
}
function aOrAn(word) { return esc((/^[aeiou]/i.test(word) ? "an " : "a ") + word); }
function titleHtml(it) {
  if (S.renaming === it.id) {
    return '<div class="tt" style="flex:1"><input class="rename-input" type="text" maxlength="80" data-rename-input="' + esc(it.id) + '" value="' + esc(it.custom_name || "") + '" placeholder="' + esc(it.base_name || it.folder || "") + '" aria-label="Custom name for ' + esc(it.folder || it.name) + '" spellcheck="false" autocomplete="off">' +
      '<span class="rename-hint">Enter saves · Esc cancels · empty clears</span><div class="folder-sub">' + (subLine(it) || esc(it.folder || it.name)) + "</div></div>";
  }
  const sub = subLine(it);
  return '<div class="tt"><div class="tt-row"><h3 data-rename="' + esc(it.id) + '" title="Double-click to rename">' + esc(it.name) + '</h3>' +
    '<button type="button" class="rename-btn" data-rename-btn="' + esc(it.id) + '" aria-label="Rename ' + esc(it.name) + '" title="Custom name">' + PENCIL + "</button></div>" +
    (sub ? '<div class="folder-sub" title="' + esc(it.rel || it.folder || "") + '">' + sub + "</div>" : "") + "</div>";
}

function cardHtml(it, draggable) {
  const st = statusOf(it);
  const dot = it.running ? '<span class="dot" aria-hidden="true"></span>' : '<span class="dot off" aria-hidden="true"></span>';
  const argsVal = it.supports_args ? getArgs(it) : "";
  let actions = "";
  if (it.url) actions += '<a class="btn sm" href="' + esc(it.url) + '" target="_blank" rel="noopener">Open ' + OPEN + '</a>';
  if (it.running) {
    actions += '<button type="button" class="btn sm" data-act="restart" title="Restart">' + RESTART_ICO + ' Restart</button>';
    actions += '<button type="button" class="btn sm" data-act="stop">' + STOP + ' Stop</button>';
  } else if (it.startable) {
    actions += '<button type="button" class="btn primary" data-act="start">' + PLAY + ' ' + (it.web ? "Start" : "Run") + '</button>';
  }

  let cmd = "";
  if (it.static) {
    const note = (it.html_files && it.html_files.length === 1) ? ' <span class="cmd-args">· ' + esc(it.html_files[0]) + '</span>' : "";
    cmd = '<div class="cmd"><span class="cmd-base">' + esc(it.cmd || "python -m http.server") + '</span>' + note + '</div>';
  } else if (it.cmd) {
    cmd = '<div class="cmd"><span class="cmd-base">' + esc(it.cmd) + '</span><span class="cmd-args">' + (argsVal ? " " + esc(argsVal) : "") + '</span></div>';
  } else if (it.needs_choice) {
    cmd = '<div class="cmd pick">Choose ' + aOrAn((it.choice_label || "command").toLowerCase()) + ' to run below.</div>';
  } else {
    cmd = '<div class="cmd err">' + esc(it.error || "Nothing to run") + '</div>';
  }

  let extra = "";
  if (it.missing_tool && it.error) extra += '<div class="card-err" data-missing-tool="' + esc(it.missing_tool) + '">' + esc(it.error) + "</div>";
  if (it.choices && it.choices.length) {
    const copts = (it.needs_choice ? '<option value="" selected disabled>Choose ' + aOrAn((it.choice_label || "command").toLowerCase()) + '…</option>' : "") +
      it.choices.map((c) => '<option value="' + esc(c.key) + '"' + (c.key === it.choice ? " selected" : "") + ">" + esc(c.label) + "</option>").join("");
    extra += '<div class="field"><label class="field-lbl" for="choice-' + esc(it.id) + '">' + esc(it.choice_label || "Run") + '</label>' +
      '<select class="card-select" id="choice-' + esc(it.id) + '" data-choice-pick="' + esc(it.id) + '">' + copts + "</select></div>";
  }
  if (it.deps_missing && it.install_cmd) {
    const blocked = it.install_missing_tool;
    extra += '<div class="deps" data-deps-for="' + esc(it.id) + '"><span>Dependencies not installed · <code>' + esc(it.install_cmd) + "</code></span>" +
      '<button type="button" class="btn xs" data-install="' + esc(it.id) + '"' + (blocked || it.running ? " disabled" : "") +
      ' title="' + esc(blocked ? blocked + " isn't installed" : "Runs " + it.install_cmd + " here; output shows below") + '">Install</button></div>';
  }
  if (it.static && it.html_files && it.html_files.length > 1) {
    const opts = it.html_files.map((f) =>
      '<option value="' + esc(f) + '"' + (f === it.html_file ? " selected" : "") + '>' + esc(f) + "</option>"
    ).join("");
    extra += '<div class="field"><label class="field-lbl" for="html-' + esc(it.id) + '">Page</label>' +
      '<select class="card-select" id="html-' + esc(it.id) + '" data-html-pick="' + esc(it.id) + '">' + opts + "</select></div>";
  }
  if (it.supports_args) {
    extra += '<div class="field"><label class="field-lbl" for="args-' + esc(it.id) + '">Args</label>' +
      '<input class="args-input" id="args-' + esc(it.id) + '" type="text" data-args-for="' + esc(it.id) + '" value="' + esc(argsVal) + '" placeholder="e.g. -reset" spellcheck="false" autocomplete="off">' +
      '<button type="button" class="btn xs" data-preset-open="' + esc(it.id) + '" title="Save args as named preset">+ Preset</button></div>';
    if (S.presetOpen[it.id]) {
      extra += '<div class="preset-save-row">' +
        '<input class="args-input" type="text" data-preset-name-for="' + esc(it.id) + '" placeholder="Preset name (e.g. reset)" spellcheck="false" autocomplete="off">' +
        '<button type="button" class="btn xs primary" data-preset-save="' + esc(it.id) + '">Save</button>' +
        '<button type="button" class="btn xs" data-preset-cancel="' + esc(it.id) + '" aria-label="Cancel preset">✕</button></div>';
    }
    const presets = it.presets || [];
    if (presets.length) {
      const matched = presets.find((pr) => pr.args === argsVal);
      const popts = '<option value="">Presets…</option>' + presets.map((pr) =>
        '<option value="' + esc(pr.name) + '"' + (matched && matched.name === pr.name ? " selected" : "") + '>' + esc(pr.name + " (" + (pr.args || "none") + ")") + "</option>"
      ).join("");
      extra += '<div class="field"><label class="field-lbl" for="preset-' + esc(it.id) + '">Preset</label>' +
        '<select class="card-select" id="preset-' + esc(it.id) + '" data-preset-pick="' + esc(it.id) + '">' + popts + "</select>" +
        '<button type="button" class="btn xs" data-preset-del="' + esc(it.id) + '" title="Delete selected preset" aria-label="Delete preset"' + (matched ? "" : " disabled") + ">✕</button></div>";
    }
  }
  if (it.url) {
    extra += '<div class="url-row"><a href="' + esc(it.url) + '" target="_blank" rel="noopener">' + esc(it.url) + "</a></div>";
  }
  if (it.running) {
    extra += '<div class="health" data-health-for="' + esc(it.id) + '">' + healthInner(it, false) + "</div>";
  } else if (it.last_run && !it.has_run) {
    const lr = it.last_run;
    const bad = lr.status === "failed";
    extra += '<div class="lastrun' + (bad ? " bad" : "") + '">Last run ' + esc(fmtAgo(lr.started)) + " · " +
      esc(lr.status === "failed" ? "exit " + lr.exit_code : lr.status) + "</div>";
  }
  if (it.startable) {
    const envVars = it.env_vars || [];
    const envOpen = !!S.envOpen[it.id];
    const ns = (S.settings && S.settings.notify) || { enabled: false, all_items: true };
    const nEff = it.notify === null || it.notify === undefined ? ns.all_items : it.notify;
    const nLabel = "Notify" + (it.notify === null || it.notify === undefined ? " · auto" : "");
    const nTitle = (ns.enabled ? "" : "Notifications are off in settings. ") +
      (it.notify === null || it.notify === undefined ? "Follows the global setting. Click to choose on/off." : "Click to cycle: on → off → auto.");
    extra += '<div class="card-tools">' +
      '<button type="button" class="pill-btn" data-toggle-env="' + esc(it.id) + '" aria-expanded="' + envOpen + '">Env' + (envVars.length ? " (" + envVars.length + ")" : "") + "</button>" +
      '<button type="button" class="pill-btn" data-toggle-crash="' + esc(it.id) + '" aria-pressed="' + (!!it.auto_restart_crash) + '" title="Auto-restart on non-zero exit (max 3 in 60s)">On crash</button>' +
      '<button type="button" class="pill-btn" data-toggle-watch="' + esc(it.id) + '" aria-pressed="' + (!!it.auto_restart_watch) + '" title="Auto-restart when files in this folder change">On change</button>' +
      '<button type="button" class="pill-btn' + (it.notify == null ? " auto" : "") + (ns.enabled ? "" : " muted") + '" data-toggle-notify="' + esc(it.id) + '" aria-pressed="' + (!!nEff) + '" title="' + esc(nTitle) + '">' + nLabel + "</button>" +
      ((it.last_run || it.has_run) ? '<button type="button" class="pill-btn" data-history="' + esc(it.id) + '" title="Past runs and logs">History' + (it.last_run ? " (" + it.last_run.count + ")" : "") + "</button>" : "") +
      (it.readme ? '<button type="button" class="pill-btn" data-readme="' + esc(it.id) + '" title="' + esc(it.readme.name) + '">README</button>' : "") +
      "</div>";
    if (envOpen) {
      const revealed = !!S.envReveal[it.id] && (envVars.length === 0 || !!S.envValues[it.id]);
      if (!revealed && envVars.length > 0) {
        const rows = envVars.map((ev) =>
          '<div class="env-mask-row"><span class="env-k">' + esc(ev.key) + '</span><span class="env-v">••••••••</span></div>'
        ).join("");
        extra += '<div class="env-box"><div class="env-head"><span>Env (masked)</span><div style="display:flex;gap:6px">' +
          '<button type="button" class="btn xs" data-env-reveal="' + esc(it.id) + '">Reveal & Edit</button>' +
          '<button type="button" class="btn xs ghost" data-toggle-env="' + esc(it.id) + '">Close</button></div></div>' +
          '<div class="env-mask-list">' + rows + "</div></div>";
      } else {
        const txt = S.envDraft[it.id] !== undefined ? S.envDraft[it.id] : envToText(envVars, S.envValues[it.id]);
        extra += '<div class="env-box"><div class="env-head"><span>Env (KEY=VALUE)</span><div style="display:flex;gap:6px">' +
          (envVars.length ? '<button type="button" class="btn xs" data-env-reveal="' + esc(it.id) + '">Mask</button>' : "") +
          '</div></div>' +
          '<textarea class="env-textarea" data-env-text-for="' + esc(it.id) + '" rows="3" placeholder="API_KEY=secret&#10;DEBUG=1" spellcheck="false">' + esc(txt) + "</textarea>" +
          '<div style="display:flex;gap:6px;justify-content:flex-end">' +
          '<button type="button" class="btn xs" data-env-cancel="' + esc(it.id) + '">Cancel</button>' +
          '<button type="button" class="btn xs primary" data-env-save="' + esc(it.id) + '">Save Env</button></div></div>';
      }
    }
  } else if (it.readme) {
    extra += '<div class="card-tools"><button type="button" class="pill-btn" data-readme="' + esc(it.id) + '" title="' + esc(it.readme.name) + '">README</button></div>';
  }

  if (it.agents && it.agents.length) {
    extra = '<div class="agent-chips">' + it.agents.map((a) => '<button type="button" class="agent-chip" data-agent-chip="' + a.pid + '" title="' + esc(a.name + " is running in this folder (PID " + a.pid + ")") + '"><span class="dot" aria-hidden="true"></span>' + esc(a.chip) + "</button>").join("") + "</div>" + extra;
  }
  if (it.note) extra = '<div class="ai-note">' + esc(it.note) + "</div>" + extra;
  const aiLbl = it.ai && it.ai.label ? it.ai.label : "";
  const kindBadge = '<span class="badge' + (aiLbl && aiLbl.toUpperCase() === String(it.kind).toUpperCase() ? " ai" : "") + '">' + esc(it.kind) + "</span>" +
    (aiLbl && aiLbl.toUpperCase() !== String(it.kind).toUpperCase() ? '<span class="badge ai">' + esc(aiLbl) + "</span>" : "");
  const aiTags = it.ai && it.ai.tags && it.ai.tags.length ? '<div class="ai-tags">' + esc(it.ai.tags.join(" · ")) + "</div>" : "";
  const restartPill = it.restart_count > 0 ? '<span class="restart-pill" title="Auto-restart count">↺ ' + it.restart_count + "</span>" : "";
  const unpin = draggable ? '<button type="button" class="unpin" data-act="unpin" aria-label="Unpin ' + esc(it.name) + '" title="Unpin">✕</button>' : "";
  return '<article class="card' + (S.sel === it.id ? " sel" : "") + '" data-id="' + esc(it.id) + '"' + (draggable ? ' draggable="true"' : "") + '>' +
    '<div class="row"><div class="title">' + FOLDER + titleHtml(it) + '</div><div style="display:flex;gap:4px;align-items:center">' + restartPill + kindBadge + unpin + '</div></div>' +
    '<div class="path">' + esc(it.path) + '</div>' + aiTags + cmd + extra +
    '<div class="bottom"><span class="status ' + st.cls + '">' + dot + esc(st.label) + '</span><div class="actions">' + actions + '</div></div></article>';
}

function watchGrids(its) {
  // Top-level cards first, then nested folders and monorepo packages under a small path heading.
  const top = its.filter((i) => !i.parent);
  const groups = new Map();
  its.filter((i) => i.parent).forEach((i) => {
    if (!groups.has(i.parent)) groups.set(i.parent, { ws: !!i.workspace, items: [] });
    groups.get(i.parent).items.push(i);
  });
  let html = top.length ? '<div class="grid">' + top.map((i) => cardHtml(i, false)).join("") + "</div>" : "";
  Array.from(groups.keys()).sort().forEach((k) => {
    const g = groups.get(k);
    html += '<div class="subgroup" data-subgroup="' + esc(k) + '"><div class="subgroup-head"><b>' + esc(k) + "/</b>" +
      (g.ws ? '<span class="badge">monorepo</span>' : '<span>nested</span>') + "</div>" +
      '<div class="grid">' + g.items.map((i) => cardHtml(i, false)).join("") + "</div></div>";
  });
  return html;
}

function render() {
  // sidebar
  const total = S.items.length;
  let nav = '<li><button type="button" data-src="all" aria-current="' + (S.source === "all") + '"><span>Everything</span><span class="count">' + total + '</span></button></li>';
  S.watch.forEach((w) => {
    nav += '<li><button type="button" data-src="' + esc(w.path) + '" aria-current="' + (S.source === w.path) + '"><span><span>' + esc(w.name) + '</span><span class="sub">Watching all subfolders</span></span><span class="count">' + w.count + '</span></button></li>';
  });
  nav += '<li><button type="button" data-src="pinned" aria-current="' + (S.source === "pinned") + '"><span><span>Pinned</span><span class="sub">Handpicked</span></span><span class="count">' + S.pinned + '</span></button></li>';
  $("#nav").innerHTML = nav;
  $("#serverInfo").textContent = "127.0.0.1:" + S.port + " · local only";
  $("#groupNav").innerHTML = S.groups.length ? S.groups.map((g) => {
    const st = g.status;
    let sub = g.running + "/" + g.total + " running · " + g.names.join(", ");
    let subCls = "";
    if (st && st.busy) sub = st.text;
    else if (st && st.errors && st.errors.length) { sub = st.text + ": " + st.errors.join("; "); subCls = " bad"; }
    return '<div class="grp" data-group="' + esc(g.id) + '"><div class="grp-top">' +
      '<button type="button" class="grp-name" data-group-edit="' + esc(g.id) + '" title="Edit group">' + esc(g.name) + "</button>" +
      '<div class="grp-btns">' +
      '<button type="button" class="icon-btn" data-group-act="start" data-gid="' + esc(g.id) + '" title="Start all" aria-label="Start all in ' + esc(g.name) + '">' + PLAY + "</button>" +
      '<button type="button" class="icon-btn" data-group-act="restart" data-gid="' + esc(g.id) + '" title="Restart all" aria-label="Restart all in ' + esc(g.name) + '">' + RESTART_ICO + "</button>" +
      '<button type="button" class="icon-btn" data-group-act="stop" data-gid="' + esc(g.id) + '" title="Stop all" aria-label="Stop all in ' + esc(g.name) + '">' + STOP + "</button>" +
      '</div></div><div class="grp-sub' + subCls + '">' + esc(sub) + "</div></div>";
  }).join("") : '<p class="form-note">Start a frontend and backend together.</p>';

  // chips
  const kinds = [["all", "All"], ["web", "Web"], ["python", "Python"], ["node", "Node"], ["scripts", "Scripts"], ["ai", "AI"]];
  $("#chips").innerHTML = kinds.map((k) => '<button type="button" class="chip" data-kind="' + k[0] + '" aria-pressed="' + (S.kind === k[0]) + '">' + k[1] + "</button>").join("");

  // sections (preserve active input/select focus)
  if (isEditingSections()) {
    S.pendingRender = true;
  } else {
    S.pendingRender = false;
    let html = "";
    const pinned = S.items.filter((i) => i.source === "pinned" && visible(i));
    if ((S.source === "all" || S.source === "pinned") && (S.pinned > 0 || S.source === "pinned")) {
      html += '<section class="group" aria-label="Pinned"><div class="head"><h2>Pinned</h2><span class="hint">Handpicked · drag to reorder</span></div>' +
        (pinned.length ? '<div class="grid" id="pinGrid">' + pinned.map((i) => cardHtml(i, true)).join("") + "</div>"
                       : '<p class="hint">Nothing pinned yet. Drop a folder or script above.</p>') + "</section>";
    }
    S.watch.forEach((w) => {
      if (!(S.source === "all" || S.source === w.path)) return;
      const its = S.items.filter((i) => i.source === "watch" && i.root === w.path && visible(i));
      const dsel = '<select data-watch-depth="' + esc(w.raw) + '" aria-label="Folder depth">' + [1, 2, 3, 4, 5, 6].map((n) => '<option value="' + n + '"' + (n === w.depth ? " selected" : "") + ">" + n + "</option>").join("") + "</select>";
      const opts = '<span class="watch-opts" title="How many folder levels to look into for projects">Depth ' + dsel + "</span>" +
        '<button type="button" class="pill-btn" data-watch-loose="' + esc(w.raw) + '" aria-pressed="' + (!!w.loose) + '" title="Show runnable files (.py, .sh, .js, .html) that sit directly in this folder">Loose files</button>';
      html += '<section class="group" aria-label="' + esc(w.name) + '"><div class="head"><h2>' + esc(w.name) + '</h2><span class="mono">' + esc(w.path) + ' · subfolders</span>' + opts +
        '<button type="button" class="btn ghost" data-unwatch="' + esc(w.raw) + '" style="min-height:32px;padding:0 8px">Stop watching</button></div>' +
        (!w.exists ? '<p class="hint">This folder no longer exists.</p>' : its.length ? watchGrids(its) : '<p class="hint">No matching subfolders.</p>') + "</section>";
    });
    if (!S.watch.length && !S.pinned) {
      html = '<div class="empty-state"><h2 style="font-size:18px">Nothing here yet</h2><p style="color:var(--muted)">Watch a folder to list all its subfolders, or drop folders and scripts onto this page to pin them.</p><button type="button" class="btn primary" data-open-add>Add a folder</button></div>';
    }
    $("#sections").innerHTML = html;
  }

  // running list (running first, then stopped/finished from this session)
  const active = S.items.filter((i) => i.running);
  const recent = S.items.filter((i) => !i.running && i.has_run);
  const runItems = active.concat(recent);
  $("#runningList").innerHTML = runItems.length ? runItems.map((i) => {
    const st = statusOf(i);
    const dot = i.running ? '<span class="dot" aria-hidden="true"></span>' : '<span class="dot off" aria-hidden="true"></span>';
    const subCls = i.running ? (i.prompt ? " wait" : i.url ? " url" : "") : "";
    const subText = i.running
      ? (i.prompt ? "waiting for input" : i.url ? i.url.replace(/^https?:\/\//, "") : "running · click for output")
      : (st.label + " · click to " + (i.web ? "start" : "re-run"));
    const healthRow = i.running ? '<div class="r-sub" style="font-family:var(--mono);font-size:11px" data-health-for="' + esc(i.id) + '" data-short="1">' + healthInner(i, true) + "</div>" : "";
    let rActions = "";
    if (i.running) {
      if (i.url) rActions += '<a class="btn sm" href="' + esc(i.url) + '" target="_blank" rel="noopener">Open ' + OPEN + "</a>";
      rActions += '<button type="button" class="btn sm" data-restart="' + esc(i.id) + '" title="Restart ' + esc(i.name) + '" aria-label="Restart ' + esc(i.name) + '">' + RESTART_ICO + "</button>";
      rActions += '<button type="button" class="btn sm" data-stop="' + esc(i.id) + '" aria-label="Stop ' + esc(i.name) + '">Stop</button>';
    } else {
      rActions += '<button type="button" class="btn sm primary" data-start="' + esc(i.id) + '" aria-label="Launch ' + esc(i.name) + '">' + PLAY + " " + (i.web ? "Start" : "Run") + "</button>";
    }
    const rowCls = "r" + (i.running ? " running" : " stopped") + (S.sel === i.id ? " sel" : "");
    return '<div class="' + rowCls + '" data-run-item="' + esc(i.id) + '" data-running="' + (i.running ? "1" : "0") + '" tabindex="0" role="button" aria-label="' + esc(i.name + " — " + subText) + '">' +
      '<div class="r-main">' + dot + '<div class="r-text"><span class="r-name">' + esc(i.name) + (i.restart_count > 0 ? ' <span class="restart-pill">↺ ' + i.restart_count + '</span>' : "") + '</span><div class="r-sub' + subCls + '">' + esc(subText) + "</div>" + healthRow + "</div></div>" +
      '<div class="r-actions">' + rActions + "</div></div>";
  }).join("") : '<p class="none">Nothing running.</p>';
}

// ---------------------------------------------------------------- terminal
const term = { lines: [""], col: 0, raf: 0 };
function termReset() { term.lines = [""]; term.col = 0; }
function termWrite(s) {
  for (const ch of s) {
    if (ch === "\n") { term.lines.push(""); term.col = 0; }
    else if (ch === "\r") term.col = 0;
    else if (ch === "\b") { if (term.col > 0) term.col--; }
    else if (ch === "\x07") {}
    else { const i = term.lines.length - 1, ln = term.lines[i]; term.lines[i] = ln.slice(0, term.col) + ch + ln.slice(term.col + 1); term.col++; }
  }
  if (term.lines.length > 5000) term.lines.splice(0, term.lines.length - 5000);
}
function scheduleTerm() { if (!term.raf) term.raf = requestAnimationFrame(termRender); }
function shownLines() { return S.past ? S.past.lines : term.lines; }
function termRender() {
  term.raf = 0;
  const out = $("#out");
  const near = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  out.classList.remove("empty");
  const lines = shownLines();
  const q = S.filter.toLowerCase();
  if (q) {
    const hits = lines.filter((l) => l.toLowerCase().includes(q));
    $("#termHits").textContent = hits.length + " match" + (hits.length === 1 ? "" : "es");
    out.innerHTML = hits.map((l) => {
      let html = "", i = 0; const low = l.toLowerCase();
      for (let j = low.indexOf(q); j !== -1; j = low.indexOf(q, i)) { html += esc(l.slice(i, j)) + "<mark>" + esc(l.slice(j, j + q.length)) + "</mark>"; i = j + q.length; }
      return html + esc(l.slice(i));
    }).join("\n") || "";
  } else {
    $("#termHits").textContent = "";
    out.textContent = lines.join("\n");
  }
  if (near || S.past) { if (!S.past || S.pastJustOpened) out.scrollTop = out.scrollHeight; S.pastJustOpened = false; }
  renderTermHeader();
}
function selItem() { return S.items.find((i) => i.id === S.sel) || null; }
function renderTermHeader() {
  const it = selItem();
  const stateEl = $("#termState");
  if (!it && S.aiRun && S.esId === S.aiRun.id) {
    $("#termName").textContent = S.aiRun.name; $("#termCmd").textContent = ""; stateEl.textContent = "Model server · started from the AI panel"; stateEl.className = "state";
    $("#termRestart").style.display = "none"; $("#termStop").style.display = "none"; $("#yn").classList.remove("show");
    $("#termHistory").disabled = true; $("#replyText").disabled = true; $("#replyBtn").disabled = true;
    return;
  }
  if (!it) {
    $("#termName").textContent = "Output"; $("#termCmd").textContent = ""; stateEl.textContent = ""; stateEl.className = "state";
    $("#termRestart").style.display = "none"; $("#termStop").style.display = "none"; $("#yn").classList.remove("show");
    $("#termHistory").disabled = true;
    $("#replyText").disabled = true; $("#replyBtn").disabled = true;
    return;
  }
  const st = statusOf(it);
  const tail = term.lines[term.lines.length - 1] || "";
  const asking = it.running && YN.test(tail);
  $("#termName").textContent = it.name; $("#termCmd").textContent = it.run_cmd || it.cmd || "";
  stateEl.textContent = asking ? "Waiting for your answer" : st.label;
  stateEl.className = "state " + (asking || st.cls === "wait" ? "wait" : st.cls);
  $("#termRestart").style.display = it.running ? "" : "none";
  $("#termStop").style.display = it.running ? "" : "none";
  $("#termHistory").disabled = !(it.last_run || it.has_run);
  $("#yn").classList.toggle("show", asking);
  $("#replyText").disabled = !it.running; $("#replyBtn").disabled = !it.running;
}
function closeStream() { if (S.es) { S.es.close(); S.es = null; } S.esId = null; S.esSeq = 0; }
function openStream(id, seq) {
  closeStream(); termReset();
  const out = $("#out"); out.classList.remove("empty"); out.textContent = "";
  const es = new EventSource("/api/items/" + encodeURIComponent(id) + "/stream");
  es.onmessage = (e) => { try { termWrite(JSON.parse(e.data).t); scheduleTerm(); } catch (x) {} };
  es.addEventListener("exit", () => { es.close(); setTimeout(refresh, 200); });
  S.es = es; S.esId = id;
  if (seq !== undefined) S.esSeq = seq;
  else { const it = S.items.find((x) => x.id === id); S.esSeq = it ? (it.run_seq || 0) : 0; }
}
function syncStream() {
  const it = selItem();
  if (!it) return;
  if (it.has_run && (S.esId !== it.id || (it.run_seq && S.esSeq !== it.run_seq) || (S.es && S.es.readyState === 2 && it.running))) {
    openStream(it.id, it.run_seq);
  }
}
function exitPast() {
  if (!S.past) return;
  S.past = null;
  $("#past").classList.remove("show");
  termRender();
}
function select(id) {
  S.aiRun = null;
  if (S.past) { S.past = null; $("#past").classList.remove("show"); }
  S.sel = id;
  const it = selItem();
  if (it && it.has_run) openStream(id, it.run_seq);
  else { closeStream(); termReset(); const out = $("#out"); out.classList.add("empty"); out.textContent = it && it.startable ? "Not started yet. Press " + (it.web ? "Start" : "Run") + " on the card." : (it ? (it.error || "") : ""); }
  render(); renderTermHeader();
}

// ---------------------------------------------------------------- actions
async function itemAction(id, action, body) {
  const r = await api("/api/items/" + encodeURIComponent(id) + "/" + action, "POST", body || {});
  if (!r.ok) notify(r.data.error || "Something went wrong.");
  return r;
}
function buildStartBody(id) {
  const it = S.items.find((x) => x.id === id);
  const body = {};
  if (it && it.supports_args) {
    const inp = document.querySelector('input[data-args-for="' + CSS.escape(id) + '"]');
    const args = inp ? inp.value : getArgs(it);
    S.argsDraft[id] = args;
    body.args = args;
  }
  if (it && it.static) {
    const sel = document.querySelector('select[data-html-pick="' + CSS.escape(id) + '"]');
    if (sel) body.html_file = sel.value;
    else if (it.html_file) body.html_file = it.html_file;
  }
  return body;
}
async function startItem(id, force) {
  const body = buildStartBody(id);
  if (force) body.force = true;
  const r = await api("/api/items/" + encodeURIComponent(id) + "/start", "POST", body);
  if (!r.ok) {
    if (r.status === 409 && r.data.error === "port_in_use") {
      const it = S.items.find((x) => x.id === id);
      const who = r.data.pid ? " by " + (r.data.command || "a process") + " (PID " + r.data.pid + ")" : "";
      notify("Port " + r.data.port + " is already in use" + who + ". " + (it ? it.name : "This item") +
        " usually listens there, so it may fail or pick another port. Nothing was stopped." +
        (r.data.pid ? " To free it yourself: kill " + r.data.pid : ""),
        [{ label: "Start anyway", run: () => startItem(id, true) }]);
    } else notify(r.data.error || "Something went wrong.");
    return;
  }
  S.pendingRender = false;
  await refresh();
  S.sel = id; openStream(id); render(); renderTermHeader();
}
async function restartItem(id) {
  const r = await itemAction(id, "restart", buildStartBody(id));
  if (!r.ok) return;
  S.pendingRender = false;
  await refresh();
  S.sel = id; openStream(id); render(); renderTermHeader();
}
async function stopItem(id) { await itemAction(id, "stop"); setTimeout(refresh, 350); }
async function send(text) {
  if (!S.sel) return;
  await itemAction(S.sel, "input", { text });
  setTimeout(refresh, 300);
}

async function savePreset(id) {
  const nameInp = document.querySelector('input[data-preset-name-for="' + CSS.escape(id) + '"]');
  const pname = nameInp ? nameInp.value.trim() : "";
  if (!pname) { notify("Enter a preset name."); return; }
  const it = S.items.find((x) => x.id === id);
  const argsInp = document.querySelector('input[data-args-for="' + CSS.escape(id) + '"]');
  const pargs = argsInp ? argsInp.value : (it ? getArgs(it) : "");
  const r = await itemAction(id, "prefs", { save_preset: { name: pname, args: pargs } });
  if (r.ok) {
    S.presetOpen[id] = false;
    S.argsDraft[id] = pargs;
    S.pendingRender = false;
    await refresh();
  }
}

async function saveEnv(id) {
  const ta = document.querySelector('textarea[data-env-text-for="' + CSS.escape(id) + '"]');
  const text = ta ? ta.value : "";
  const r = await itemAction(id, "prefs", { env_text: text });
  if (r.ok) {
    delete S.envDraft[id];
    delete S.envValues[id];
    S.envReveal[id] = false;
    S.pendingRender = false;
    await refresh();
  }
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button, a, input, select, textarea, label, [data-run-item], article.card");
  if (!t) return;
  if (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.tagName === "LABEL") return;
  const card = e.target.closest("article.card");
  // double-click on a title: the first click re-renders the card, so a native dblclick never fires
  const ttl = e.target.closest("h3[data-rename]");
  if (ttl && e.detail === 2) { startRename(ttl.dataset.rename); return; }
  if (t.dataset.src) { S.source = t.dataset.src; render(); return; }
  if (t.dataset.kind) { S.kind = t.dataset.kind; render(); return; }
  if (t.dataset.unwatch) { api("/api/unwatch", "POST", { path: t.dataset.unwatch }).then((r) => { applyListing(r.data); if (S.source !== "all" && S.source !== "pinned") { S.source = "all"; render(); } }); return; }
  if (t.dataset.sel) { select(t.dataset.sel); return; }
  if (t.dataset.stop) { stopItem(t.dataset.stop); return; }
  if (t.dataset.restart) { restartItem(t.dataset.restart); return; }
  if (t.dataset.start) { startItem(t.dataset.start); return; }
  if (t.dataset.runItem) {
    if (t.dataset.running === "1") select(t.dataset.runItem);
    else startItem(t.dataset.runItem);
    return;
  }
  if (t.dataset.presetOpen) {
    const id = t.dataset.presetOpen;
    S.presetOpen[id] = !S.presetOpen[id];
    render();
    if (S.presetOpen[id]) {
      const inp = document.querySelector('input[data-preset-name-for="' + CSS.escape(id) + '"]');
      if (inp) inp.focus();
    }
    return;
  }
  if (t.dataset.presetCancel) { S.presetOpen[t.dataset.presetCancel] = false; render(); return; }
  if (t.dataset.presetSave) { await savePreset(t.dataset.presetSave); return; }
  if (t.dataset.presetDel) {
    const id = t.dataset.presetDel;
    const sel = document.querySelector('select[data-preset-pick="' + CSS.escape(id) + '"]');
    const pname = sel ? sel.value : "";
    if (!pname) return;
    const r = await itemAction(id, "prefs", { delete_preset: pname });
    if (r.ok) { S.pendingRender = false; await refresh(); }
    return;
  }
  if (t.dataset.toggleEnv) {
    const id = t.dataset.toggleEnv;
    S.envOpen[id] = !S.envOpen[id];
    if (!S.envOpen[id]) { S.envReveal[id] = false; delete S.envDraft[id]; delete S.envValues[id]; }
    render();
    return;
  }
  if (t.dataset.envReveal) {
    const id = t.dataset.envReveal;
    if (!S.envReveal[id]) {
      if (!(await loadEnvValues(id))) return;
      S.envReveal[id] = true;
    } else {
      S.envReveal[id] = false;
      delete S.envValues[id];
    }
    delete S.envDraft[id];
    render();
    return;
  }
  if (t.dataset.envCancel) {
    const id = t.dataset.envCancel;
    S.envReveal[id] = false;
    delete S.envDraft[id];
    delete S.envValues[id];
    const it = S.items.find((x) => x.id === id);
    if (!it || !(it.env_vars && it.env_vars.length)) S.envOpen[id] = false;
    render();
    return;
  }
  if (t.dataset.envSave) { await saveEnv(t.dataset.envSave); return; }
  if (t.dataset.toggleCrash) {
    const id = t.dataset.toggleCrash;
    const it = S.items.find((x) => x.id === id);
    if (!it) return;
    const r = await itemAction(id, "prefs", { auto_restart_crash: !it.auto_restart_crash });
    if (r.ok) { S.pendingRender = false; await refresh(); }
    return;
  }
  if (t.dataset.toggleNotify) {
    const id = t.dataset.toggleNotify;
    const it = S.items.find((x) => x.id === id);
    if (!it) return;
    const ns = (S.settings && S.settings.notify) || { all_items: true };
    const next = it.notify == null ? !ns.all_items : (it.notify === true ? false : null);
    const r = await itemAction(id, "prefs", { notify: next });
    if (r.ok) { S.pendingRender = false; await refresh(); }
    return;
  }
  if (t.dataset.history) { openHistory(t.dataset.history); return; }
  if (t.dataset.renameBtn) { startRename(t.dataset.renameBtn); return; }
  if (t.dataset.install) { installItem(t.dataset.install); return; }
  if (t.dataset.watchLoose) {
    const w = S.watch.find((x) => x.raw === t.dataset.watchLoose);
    const r = await api("/api/watch-opts", "POST", { path: t.dataset.watchLoose, loose: !(w && w.loose) });
    if (r.ok) applyListing(r.data);
    return;
  }
  if (t.dataset.readme) { openReadme(t.dataset.readme); return; }
  if (t.dataset.groupAct) { groupAction(t.dataset.gid, t.dataset.groupAct); return; }
  if (t.dataset.groupEdit) { openGroupDialog(t.dataset.groupEdit); return; }
  if (t.dataset.toggleWatch) {
    const id = t.dataset.toggleWatch;
    const it = S.items.find((x) => x.id === id);
    if (!it) return;
    const r = await itemAction(id, "prefs", { auto_restart_watch: !it.auto_restart_watch });
    if (r.ok) { S.pendingRender = false; await refresh(); }
    return;
  }
  if (t.hasAttribute("data-open-add")) { openDialog(); return; }
  if (card && t.dataset.act) {
    const id = card.dataset.id;
    if (t.dataset.act === "start") startItem(id);
    else if (t.dataset.act === "restart") restartItem(id);
    else if (t.dataset.act === "stop") stopItem(id);
    else if (t.dataset.act === "unpin") api("/api/unpin", "POST", { id }).then((r) => { applyListing(r.data); });
    return;
  }
  if (card && t.tagName !== "A") select(card.dataset.id);
});

document.addEventListener("input", (e) => {
  const t = e.target;
  if (!t || !t.matches) return;
  if (t.matches("input[data-args-for]")) {
    const id = t.dataset.argsFor;
    S.argsDraft[id] = t.value;
    const card = t.closest("article.card");
    const preview = card && card.querySelector(".cmd-args");
    if (preview) preview.textContent = t.value ? " " + t.value : "";
  } else if (t.matches("textarea[data-env-text-for]")) {
    S.envDraft[t.dataset.envTextFor] = t.value;
  }
});

document.addEventListener("change", async (e) => {
  const t = e.target;
  if (!t || !t.matches) return;
  if (t.matches("select[data-choice-pick]")) {
    const id = t.dataset.choicePick;
    const r = await itemAction(id, "prefs", { choice: t.value });
    t.blur();
    if (r.ok) { S.pendingRender = false; await refresh(); } else notify((r.data && r.data.error) || "Could not save that choice.");
    return;
  }
  if (t.matches("select[data-watch-depth]")) {
    const r = await api("/api/watch-opts", "POST", { path: t.dataset.watchDepth, depth: Number(t.value) });
    t.blur();
    if (r.ok) { S.pendingRender = false; applyListing(r.data); }
    return;
  }
  if (t.matches("select[data-html-pick]")) {
    const id = t.dataset.htmlPick;
    const r = await itemAction(id, "prefs", { html_file: t.value });
    t.blur();
    if (r.ok) { S.pendingRender = false; await refresh(); }
  } else if (t.matches("input[data-args-for]")) {
    const id = t.dataset.argsFor;
    S.argsDraft[id] = t.value;
    const r = await itemAction(id, "prefs", { args: t.value });
    if (r.ok) await refresh();
  } else if (t.matches("select[data-preset-pick]")) {
    const id = t.dataset.presetPick;
    const it = S.items.find((x) => x.id === id);
    const pr = it && (it.presets || []).find((p) => p.name === t.value);
    t.blur();
    if (pr) {
      S.argsDraft[id] = pr.args;
      render();
      const r = await itemAction(id, "prefs", { args: pr.args });
      if (r.ok) { S.pendingRender = false; await refresh(); }
    }
  }
});

document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && t.matches) {
    if (t.matches("input[data-args-for]") && e.key === "Enter") {
      e.preventDefault();
      const id = t.dataset.argsFor;
      S.argsDraft[id] = t.value;
      t.blur();
      startItem(id);
      return;
    }
    if (t.matches("input[data-rename-input]")) {
      if (e.key === "Enter") { e.preventDefault(); saveRename(t.dataset.renameInput, t.value); return; }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancelRename(); return; }
    }
    if (t.matches("input[data-preset-name-for]") && e.key === "Enter") {
      e.preventDefault();
      savePreset(t.dataset.presetNameFor);
      return;
    }
    if (t.matches("#runningList .r[data-run-item]") && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      const id = t.dataset.runItem;
      if (t.dataset.running === "1") select(id);
      else startItem(id);
      return;
    }
  }
});

document.addEventListener("focusout", () => {
  if (S.pendingRender) {
    setTimeout(() => { if (S.pendingRender && !isEditingSections()) { S.pendingRender = false; render(); } }, 0);
  }
});

$("#termRestart").onclick = () => S.sel && restartItem(S.sel);
$("#termStop").onclick = () => S.sel && stopItem(S.sel);

// ---------------------------------------------------------------- command palette & shortcuts
const palDlg = $("#paletteDlg");
const helpDlg = $("#helpDlg");

function fuzzyScore(q, text) {
  if (!q) return 1;
  const nq = q.toLowerCase();
  const nt = text.toLowerCase();
  const idx = nt.indexOf(nq);
  if (idx !== -1) return 1000 - idx * 2 - (nt.length - nq.length) * 0.1;
  let qi = 0, score = 0, run = 0;
  for (let i = 0; i < nt.length && qi < nq.length; i++) {
    if (nt[i] === nq[qi]) {
      qi++;
      run++;
      score += 10 + run * 4;
      if (i === 0 || nt[i - 1] === "-" || nt[i - 1] === "_" || nt[i - 1] === " " || nt[i - 1] === "/") score += 8;
    } else {
      run = 0;
    }
  }
  return qi === nq.length ? score : -1;
}

function renderPalette() {
  const q = ($("#paletteInput").value || "").trim();
  const scored = S.items.map((it) => {
    const hay = [it.name, it.base_name || "", it.folder || "", it.rel || "", it.kind, it.path, it.cmd || ""].join(" ");
    const sc = fuzzyScore(q, hay);
    return { it, sc };
  }).filter((x) => x.sc > 0);
  scored.sort((a, b) => (b.sc - a.sc) || ((b.it.running ? 1 : 0) - (a.it.running ? 1 : 0)) || a.it.name.localeCompare(b.it.name));
  const gscored = (S.groups || []).map((g) => ({ it: { _group: g, id: "group:" + g.id, name: g.name }, sc: fuzzyScore(q, "group stack " + g.name + " " + g.names.join(" ")) })).filter((x) => x.sc > 0);
  const aiRows = [];
  if (AI.data) {
    AI.data.servers.forEach((sv) => aiRows.push({ it: { _ai: "server", s: sv, id: "ai:" + sv.id, name: sv.name }, sc: fuzzyScore(q, "ai model server llm " + sv.name + " " + sv.models.map((m) => m.name).join(" ")) }));
    AI.data.agents.filter((a) => !a.model).forEach((a) => aiRows.push({ it: { _ai: "agent", a: a, id: "agent:" + a.pid, name: a.name }, sc: fuzzyScore(q, "ai agent " + a.name + " " + a.key + " " + (a.cwd || "") + " " + (a.item_name || "")) }));
    aiRows.push({ it: { _ai: "panel", id: "ai:panel", name: "AI on this machine" }, sc: fuzzyScore(q, "ai panel models agents ollama lm studio") * (q ? 1 : 0.001) });
  }
  const all = scored.concat(gscored, aiRows.filter((x) => x.sc > 0));
  all.sort((a, b) => b.sc - a.sc);
  scored.length = 0; Array.prototype.push.apply(scored, all);
  S.palItems = scored.map((x) => x.it);
  if (S.palIdx >= S.palItems.length) S.palIdx = Math.max(0, S.palItems.length - 1);
  if (!S.palItems.length) {
    $("#paletteList").innerHTML = '<p class="none" style="padding:16px">No matching items.</p>';
    return;
  }
  $("#paletteList").innerHTML = S.palItems.map((it, idx) => {
    if (it._ai) return palAiRow(it, idx);
    if (it._group) {
      const g = it._group;
      const activeG = idx === S.palIdx ? " active" : "";
      return '<div class="pal-item' + activeG + '" role="option" aria-selected="' + (idx === S.palIdx) + '" data-pal-row="' + idx + '">' +
        '<div class="pal-left"><div class="pal-title"><span>' + esc(g.name) + '</span><span class="badge">GROUP</span></div>' +
        '<div class="pal-sub">' + esc(g.running + "/" + g.total + " running · " + g.names.join(" → ")) + "</div></div>" +
        '<div class="pal-actions">' +
        '<button type="button" class="btn xs primary" data-pal-group="start" data-gid="' + esc(g.id) + '">Start all</button>' +
        '<button type="button" class="btn xs" data-pal-group="restart" data-gid="' + esc(g.id) + '">Restart all</button>' +
        '<button type="button" class="btn xs" data-pal-group="stop" data-gid="' + esc(g.id) + '">Stop all</button></div></div>';
    }
    const st = statusOf(it);
    const active = idx === S.palIdx ? " active" : "";
    let acts = "";
    if (it.running) {
      if (it.url) acts += '<a class="btn xs" href="' + esc(it.url) + '" target="_blank" rel="noopener">Open ' + OPEN + '</a>';
      acts += '<button type="button" class="btn xs" data-pal-act="select" data-pal-id="' + esc(it.id) + '">Select</button>';
      acts += '<button type="button" class="btn xs" data-pal-act="stop" data-pal-id="' + esc(it.id) + '">Stop</button>';
    } else if (it.startable) {
      acts += '<button type="button" class="btn xs primary" data-pal-act="start" data-pal-id="' + esc(it.id) + '">' + (it.web ? "Start" : "Run") + "</button>";
    } else {
      acts += '<button type="button" class="btn xs" data-pal-act="select" data-pal-id="' + esc(it.id) + '">Select</button>';
    }
    if (it.readme) acts += '<button type="button" class="btn xs" data-pal-act="readme" data-pal-id="' + esc(it.id) + '">README</button>';
    return '<div class="pal-item' + active + '" role="option" aria-selected="' + (idx === S.palIdx) + '" data-pal-row="' + idx + '">' +
      '<div class="pal-left"><div class="pal-title"><span>' + esc(it.name) + '</span>' + (it.custom_name ? '<span class="pal-sub" style="flex:0 1 auto">' + esc(it.rel || it.folder || "") + "</span>" : "") + '<span class="badge">' + esc(it.kind) + '</span></div>' +
      '<div class="pal-sub">' + esc(st.label + " · " + (it.url || it.path)) + "</div></div>" +
      '<div class="pal-actions">' + acts + "</div></div>";
  }).join("");
  const actEl = $("#paletteList .pal-item.active");
  if (actEl && actEl.scrollIntoView) actEl.scrollIntoView({ block: "nearest" });
}

function palAiRow(it, idx) {
  const active = idx === S.palIdx ? " active" : "";
  let title = "", badge = "", sub = "", acts = "";
  if (it._ai === "server") {
    const sv = it.s;
    title = sv.name; badge = "MODEL SERVER";
    sub = sv.running ? (sv.url.replace(/^https?:\/\//, "") + " · " + sv.loaded + " loaded / " + sv.models.length + " models") : "not running · " + (sv.start_cmd || "");
    if (sv.running) acts += '<a class="btn xs" href="' + esc(sv.url) + '" target="_blank" rel="noopener">Open ' + OPEN + "</a>";
    if (!sv.running && sv.can_start) acts += '<button type="button" class="btn xs primary" data-pal-ai="start" data-key="' + esc(sv.key) + '">Start</button>';
    if (sv.running && sv.can_stop) acts += '<button type="button" class="btn xs" data-pal-ai="stop" data-key="' + esc(sv.key) + '" data-port="' + sv.port + '">Stop</button>';
  } else if (it._ai === "agent") {
    const a = it.a;
    title = a.name; badge = "AGENT";
    sub = "PID " + a.pid + " · " + (a.cwd || "folder unknown");
    if (a.item_id) acts += '<button type="button" class="btn xs" data-pal-ai="reveal" data-item="' + esc(a.item_id) + '">Reveal folder</button>';
    acts += '<button type="button" class="btn xs" data-pal-ai="agent-stop" data-pid="' + a.pid + '" data-name="' + esc(a.name) + '">Stop</button>';
  } else {
    title = "AI on this machine"; badge = "AI"; sub = "Local model servers and running coding agents";
    acts = '<button type="button" class="btn xs" data-pal-ai="panel">Show</button>';
  }
  return '<div class="pal-item' + active + '" role="option" aria-selected="' + (idx === S.palIdx) + '" data-pal-row="' + idx + '">' +
    '<div class="pal-left"><div class="pal-title"><span>' + esc(title) + '</span><span class="badge ai">' + badge + '</span></div>' +
    '<div class="pal-sub">' + esc(sub) + "</div></div>" + '<div class="pal-actions">' + acts + "</div></div>";
}
function execPaletteAi(it, openUrl) {
  if (it._ai === "server") {
    if (it.s.running) { if (openUrl) window.open(it.s.url, "_blank", "noopener"); else openAI("models"); }
    else if (it.s.can_start) aiStartServer(it.s.key);
    else openAI("models");
  } else if (it._ai === "agent") {
    if (it.a.item_id) revealItem(it.a.item_id); else { AI.flash = it.a.pid; openAI("agents"); }
  } else openAI("models");
}

function openPalette() {
  if (helpDlg.open) helpDlg.close();
  if (dlg.open) dlg.close();
  S.palIdx = 0;
  $("#paletteInput").value = "";
  renderPalette();
  if (!palDlg.open) palDlg.showModal();
  $("#paletteInput").focus();
}

function closePalette() { if (palDlg.open) palDlg.close(); }

function execPaletteItem(it, openUrl) {
  if (!it) return;
  closePalette();
  if (it._group) { groupAction(it._group.id, "start"); return; }
  if (it._ai) { execPaletteAi(it, openUrl); return; }
  if (openUrl && it.url) {
    select(it.id);
    window.open(it.url, "_blank", "noopener");
    return;
  }
  if (it.running) {
    select(it.id);
  } else if (it.startable) {
    startItem(it.id);
  } else {
    select(it.id);
  }
}

$("#cmdPaletteBtn").onclick = openPalette;
$("#paletteClose").onclick = closePalette;
$("#helpBtn").onclick = () => { if (helpDlg.open) helpDlg.close(); else helpDlg.showModal(); };
$("#helpClose").onclick = () => helpDlg.close();

$("#paletteInput").addEventListener("input", () => { S.palIdx = 0; renderPalette(); });
$("#paletteInput").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") {
    e.preventDefault();
    if (S.palItems.length) { S.palIdx = (S.palIdx + 1) % S.palItems.length; renderPalette(); }
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    if (S.palItems.length) { S.palIdx = (S.palIdx - 1 + S.palItems.length) % S.palItems.length; renderPalette(); }
  } else if (e.key === "Enter") {
    e.preventDefault();
    execPaletteItem(S.palItems[S.palIdx], e.metaKey || e.ctrlKey);
  } else if (e.key === "Escape") {
    e.preventDefault();
    closePalette();
  }
});

$("#paletteList").addEventListener("click", (e) => {
  const ab = e.target.closest("[data-pal-ai]");
  if (ab) {
    closePalette();
    const k = ab.dataset.palAi;
    if (k === "start") aiStartServer(ab.dataset.key);
    else if (k === "stop") aiStopServer(ab.dataset.key, Number(ab.dataset.port));
    else if (k === "reveal") revealItem(ab.dataset.item);
    else if (k === "agent-stop") aiStopAgent(Number(ab.dataset.pid), ab.dataset.name);
    else openAI("models");
    return;
  }
  const gb = e.target.closest("[data-pal-group]");
  if (gb) { closePalette(); groupAction(gb.dataset.gid, gb.dataset.palGroup); return; }
  const actBtn = e.target.closest("[data-pal-act]");
  if (actBtn) {
    const id = actBtn.dataset.palId;
    const act = actBtn.dataset.palAct;
    closePalette();
    if (act === "start") startItem(id);
    else if (act === "stop") stopItem(id);
    else if (act === "readme") openReadme(id);
    else select(id);
    return;
  }
  if (e.target.closest("a")) { closePalette(); return; }
  const row = e.target.closest("[data-pal-row]");
  if (row) execPaletteItem(S.palItems[Number(row.dataset.palRow)], false);
});

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (palDlg.open) closePalette();
    else openPalette();
    return;
  }
  const extraOpen = ["#histDlg", "#groupDlg", "#notifDlg", "#readmeDlg", "#aiDlg", "#confirmDlg"].some((sel) => $(sel).open);
  if (extraOpen) return;
  if (palDlg.open || helpDlg.open || dlg.open) {
    if (e.key === "Escape") {
      if (palDlg.open) closePalette();
      if (helpDlg.open) helpDlg.close();
      if (dlg.open) dlg.close();
    } else if (helpDlg.open && e.key === "?") {
      e.preventDefault();
      helpDlg.close();
    }
    return;
  }
  const ae = document.activeElement;
  const tag = ae && ae.tagName;
  const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (ae && ae.isContentEditable);
  if (typing) return;

  if ((e.metaKey || e.ctrlKey) && e.key === ".") {
    e.preventDefault();
    if (S.sel) stopItem(S.sel);
  } else if (!e.metaKey && !e.ctrlKey && !e.altKey && e.shiftKey && e.key.toLowerCase() === "h") {
    e.preventDefault();
    if (S.sel) openHistory(S.sel);
  } else if (!e.metaKey && !e.ctrlKey && !e.altKey && e.shiftKey && e.key.toLowerCase() === "r") {
    e.preventDefault();
    if (S.sel) restartItem(S.sel);
  } else if (!e.metaKey && !e.ctrlKey && !e.altKey && e.key === "?") {
    e.preventDefault();
    helpDlg.showModal();
  } else if (!e.metaKey && !e.ctrlKey && !e.altKey && e.key === "/") {
    e.preventDefault();
    $("#q").focus();
  }
});

// ---------------------------------------------------------------- README viewer
const readmeDlg = $("#readmeDlg");
let readmeState = { id: null, raw: false, data: null };
async function openReadme(id) {
  const r = await api("/api/items/" + encodeURIComponent(id) + "/readme");
  if (!r.ok) {
    notify(r.data && r.data.error === "readme too large" ? "That README is larger than 1 MB." : "No README found for this item.");
    return;
  }
  readmeState = { id, raw: false, data: r.data };
  const it = S.items.find((x) => x.id === id);
  $("#readmeTitle").textContent = (it ? it.name + " · " : "") + r.data.name;
  $("#readmeMeta").textContent = r.data.path + " · " + (r.data.size < 1024 ? r.data.size + " B" : (Math.round(r.data.size / 102.4) / 10) + " KB");
  $("#readmeReveal").hidden = !(r.data.can_reveal || r.data.can_open);
  $("#readmeOpen").hidden = !r.data.can_open;
  $("#readmeReveal").textContent = r.data.platform === "mac" ? "Show in Finder" : "Show in folder";
  $("#readmeRaw").hidden = r.data.format !== "md";
  $("#readmeRaw").textContent = "View raw";
  $("#readmeRaw").setAttribute("aria-pressed", "false");
  renderReadmeBody();
  if (palDlg.open) closePalette();
  if (!readmeDlg.open) readmeDlg.showModal();
}
function renderReadmeBody() {
  const body = $("#readmeBody");
  const d = readmeState.data;
  if (!d) { body.textContent = ""; return; }
  if (readmeState.raw || d.format !== "md") {
    body.className = "raw";
    body.textContent = d.text;
    return;
  }
  body.className = "md-view";
  const base = "/api/items/" + encodeURIComponent(readmeState.id) + "/readme/asset?path=";
  body.innerHTML = renderMarkdown(d.text, { assetBase: base }) || "<p class='form-note'>This README is empty.</p>";
}
// Remote README images are placeholders until clicked (no requests to other hosts without a click).
$("#readmeBody").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-md-img]");
  if (!b) return;
  const img = document.createElement("img");
  img.src = b.dataset.mdImg;
  img.alt = b.dataset.alt || "";
  if (b.dataset.title) img.title = b.dataset.title;
  img.referrerPolicy = "no-referrer";
  img.loading = "lazy";
  b.replaceWith(img);
});
function closeReadme() { if (readmeDlg.open) readmeDlg.close(); }
$("#readmeClose").onclick = closeReadme;
$("#readmeRaw").onclick = () => {
  if (!readmeState.data) return;
  if (readmeState.data.format !== "md") return;
  readmeState.raw = !readmeState.raw;
  $("#readmeRaw").textContent = readmeState.raw ? "View rendered" : "View raw";
  $("#readmeRaw").setAttribute("aria-pressed", String(readmeState.raw));
  renderReadmeBody();
};
$("#readmeCopy").onclick = async () => {
  const path = readmeState.data && readmeState.data.path;
  if (!path) return;
  try { await navigator.clipboard.writeText(path); $("#readmeCopy").textContent = "Copied"; setTimeout(() => { $("#readmeCopy").textContent = "Copy path"; }, 1200); }
  catch (e) { notify(path); }
};
async function revealReadme(mode) {
  if (!readmeState.id) return;
  const r = await itemAction(readmeState.id, "readme-open", { mode });
  if (!r.ok) notify((r.data && r.data.path) || r.data.error || "Could not open the file.");
}
$("#readmeReveal").onclick = () => revealReadme("reveal");
$("#readmeOpen").onclick = () => revealReadme("open");
readmeDlg.addEventListener("cancel", (e) => { e.preventDefault(); closeReadme(); });

// ---------------------------------------------------------------- custom names
function focusRename() {
  const inp = S.renaming && document.querySelector('input[data-rename-input="' + CSS.escape(S.renaming) + '"]');
  if (inp) { inp.focus(); inp.select(); }
}
function startRename(id) {
  if (S.renaming && S.renaming !== id) S.renaming = null;
  S.renaming = id;
  S.pendingRender = false;
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
  focusRename();
}
function cancelRename() {
  if (!S.renaming) return;
  S.renaming = null;
  S.pendingRender = false;
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  render();
}
async function saveRename(id, value) {
  const label = String(value || "").trim();
  S.renaming = null;
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  const r = await itemAction(id, "prefs", { label });
  if (!r.ok) notify((r.data && r.data.error) || "Could not save the name.");
  S.pendingRender = false; S.last = "";
  await refresh();
}
document.addEventListener("dblclick", (e) => {
  const h = e.target.closest("h3[data-rename]");
  if (h) { e.preventDefault(); startRename(h.dataset.rename); }
});
document.addEventListener("focusout", (e) => {
  const t = e.target;
  if (t && t.matches && t.matches("input[data-rename-input]") && S.renaming === t.dataset.renameInput) {
    setTimeout(() => { if (S.renaming === t.dataset.renameInput && document.activeElement !== t) cancelRename(); }, 120);
  }
});
async function installItem(id) {
  select(id);
  const r = await itemAction(id, "install");
  if (!r.ok) notify((r.data && r.data.error) || "Could not start the install.");
  S.last = "";
  await refresh();
}

$("#yes").onclick = () => send("y\n");
$("#no").onclick = () => send("n\n");
$("#reply").onsubmit = (e) => { e.preventDefault(); const v = $("#replyText").value; $("#replyText").value = ""; send(v + "\n"); };
$("#q").oninput = (e) => { S.q = e.target.value.trim(); render(); };

// ---------------------------------------------------------------- add dialog / pin / watch
const dlg = $("#dlg");
function openDialog() { $("#dlgMsg").textContent = ""; if (!dlg.open) dlg.showModal(); }
$("#addBtn").onclick = openDialog;
$("#watchBtn").onclick = openDialog;
$("#pasteBtn").onclick = () => { openDialog(); $("#pinPath").focus(); };
async function viaPath(url, path) {
  const r = await api(url, "POST", { path });
  if (r.ok) { applyListing(r.data); $("#dlgMsg").textContent = ""; return true; }
  $("#dlgMsg").textContent = r.status === 404 ? "That path doesn't exist." : (r.data.error || "Failed.");
  return false;
}
$("#watchGo").onclick = async () => { const v = $("#watchPath").value.trim(); if (v && await viaPath("/api/watch", v)) { $("#watchPath").value = ""; dlg.close(); } };
$("#pinGo").onclick = async () => { const v = $("#pinPath").value.trim(); if (v && await viaPath("/api/pin", v)) { $("#pinPath").value = ""; dlg.close(); } };
async function pick(mode) {
  notify("Choose a folder in the macOS dialog…");
  const r = await api("/api/pick", "POST", { mode });
  hideNotice();
  if (r.ok) { applyListing(r.data); if (dlg.open) dlg.close(); }
  else if (r.status !== 499) notify(r.data.error || "Could not pick a folder.");
}
$("#watchPick").onclick = () => pick("watch");
$("#pinPick").onclick = () => pick("pin");
$("#pickBtn").onclick = () => pick("pin");

// ---------------------------------------------------------------- drag and drop
// Files/folders dropped from Finder: browsers only reveal the NAME, so the server finds the folder in your watched folders.
const drop = $("#drop");
let dragDepth = 0;
const hasFiles = (e) => e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") !== -1;
window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { dragDepth++; drop.classList.add("drag"); } });
window.addEventListener("dragleave", (e) => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; drop.classList.remove("drag"); } });
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("drop", async (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; drop.classList.remove("drag");
  let names = Array.prototype.map.call(e.dataTransfer.files || [], (f) => f.name);
  if (!names.length && e.dataTransfer.items) {
    names = Array.prototype.map.call(e.dataTransfer.items, (it) => { const en = it.webkitGetAsEntry && it.webkitGetAsEntry(); return en ? en.name : null; }).filter(Boolean);
  }
  for (const name of names) await pinByName(name);
});
async function pinByName(name) {
  const r = await api("/api/pin", "POST", { name });
  if (r.ok) { applyListing(r.data); return; }
  if (r.status === 409) {
    notify('More than one "' + name + '" found. Which one?', (r.data.raw || []).map((p, i) => ({
      label: r.data.candidates[i], run: async () => { const x = await api("/api/pin", "POST", { path: p }); if (x.ok) applyListing(x.data); } })));
  } else if (r.status === 404) {
    notify('Couldn\'t find "' + name + '" inside your watched folders.', [
      { label: "Choose folder…", run: () => pick("pin") }, { label: "Paste a path", run: () => { openDialog(); $("#pinPath").focus(); } }]);
  } else notify(r.data.error || "Could not pin that.");
}

// reorder pinned cards (internal drag, no files involved)
document.addEventListener("dragstart", (e) => {
  if (e.target.closest && e.target.closest("input, select, textarea, button, a")) { e.preventDefault(); return; }
  const c = e.target.closest && e.target.closest("#pinGrid article.card");
  if (!c) return;
  S.dragId = c.dataset.id; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", c.dataset.id);
});
document.addEventListener("dragover", (e) => {
  if (!S.dragId) return;
  const c = e.target.closest && e.target.closest("#pinGrid article.card");
  document.querySelectorAll("#pinGrid .over").forEach((x) => x.classList.remove("over"));
  if (c && c.dataset.id !== S.dragId) { e.preventDefault(); c.classList.add("over"); }
});
document.addEventListener("drop", async (e) => {
  if (!S.dragId) return;
  const c = e.target.closest && e.target.closest("#pinGrid article.card");
  const from = S.dragId; S.dragId = null;
  document.querySelectorAll("#pinGrid .over").forEach((x) => x.classList.remove("over"));
  if (!c || c.dataset.id === from) return;
  e.preventDefault();
  const ids = Array.prototype.map.call(document.querySelectorAll("#pinGrid article.card"), (x) => x.dataset.id).filter((x) => x !== from);
  ids.splice(ids.indexOf(c.dataset.id), 0, from);
  const r = await api("/api/reorder", "POST", { ids });
  if (r.ok) applyListing(r.data);
});
document.addEventListener("dragend", () => { S.dragId = null; document.querySelectorAll("#pinGrid .over").forEach((x) => x.classList.remove("over")); });

// ---------------------------------------------------------------- terminal tools: filter, copy, clear
$("#termFilter").addEventListener("input", (e) => { S.filter = e.target.value; scheduleTerm(); });
$("#termFilter").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.target.value = ""; S.filter = ""; scheduleTerm(); } });
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) {}
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand("copy"); } catch (e) {}
  ta.remove(); return ok;
}
$("#termCopy").onclick = async () => {
  const text = shownLines().join("\n");
  const ok = await copyText(text);
  const b = $("#termCopy"); b.textContent = ok ? "Copied" : "Copy failed";
  setTimeout(() => { b.textContent = "Copy log"; }, 1500);
};
$("#termClear").onclick = () => {
  if (S.past) { exitPast(); return; }
  termReset(); $("#out").textContent = ""; scheduleTerm();
};
$("#termHistory").onclick = () => S.sel && openHistory(S.sel);
$("#pastBack").onclick = exitPast;

// ---------------------------------------------------------------- run history
const histDlg = $("#histDlg");
let histClearArmed = false;
async function openHistory(id) {
  S.histId = id;
  const it = S.items.find((x) => x.id === id);
  $("#histTitle").textContent = "Run history" + (it ? " · " + it.name : "");
  histClearArmed = false; $("#histClear").textContent = "Clear history";
  $("#histList").innerHTML = '<p class="form-note">Loading…</p>';
  if (!histDlg.open) histDlg.showModal();
  await loadHistory();
}
function statusBadge(e) {
  const cls = e.status === "finished" ? "ok" : e.status === "failed" ? "bad" : e.status === "running" ? "run" : "";
  const label = e.status === "failed" ? "exit " + e.exit_code : e.status;
  return '<span class="st-badge ' + cls + '">' + esc(label) + "</span>";
}
async function loadHistory() {
  const id = S.histId;
  const r = await api("/api/items/" + encodeURIComponent(id) + "/history");
  if (!r.ok) { $("#histList").innerHTML = '<p class="form-note">Could not load history.</p>'; return; }
  const h = r.data.history || [];
  $("#histNote").textContent = h.length ? h.length + " run" + (h.length === 1 ? "" : "s") + " kept (max 25, last 2000 lines each)" : "";
  $("#histList").innerHTML = h.length ? h.map((e) => {
    const dur = e.duration != null ? fmtDur(e.duration) : (e.started ? fmtDur(Date.now() / 1000 - e.started) + " so far" : "");
    const meta = [dur, e.preset ? "preset " + e.preset : null, e.args ? "args " + e.args : null, e.trigger && e.trigger !== "manual" ? e.trigger : null, e.port ? ":" + e.port : null].filter(Boolean).join(" · ");
    return '<div class="hist-row"><div class="hist-main"><div class="hist-when">' + esc(fmtWhen(e.started)) + statusBadge(e) + "</div>" +
      '<div class="hist-meta" title="' + esc(e.cmd || "") + '">' + esc(meta || e.cmd || "") + "</div></div>" +
      (e.log ? '<button type="button" class="btn sm" data-view-run="' + esc(e.run_id) + '">View log</button>' : "") + "</div>";
  }).join("") : '<p class="form-note">No runs saved yet.</p>';
}
$("#histList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-view-run]");
  if (!b) return;
  const id = S.histId, rid = b.dataset.viewRun;
  const r = await api("/api/items/" + encodeURIComponent(id) + "/history/" + encodeURIComponent(rid) + "/log");
  if (!r.ok) { notify("Could not load that log."); return; }
  histDlg.close();
  if (S.sel !== id) select(id);
  const en = r.data.entry;
  S.past = { id, run_id: rid, entry: en, lines: (r.data.text || "").split("\n") };
  S.pastJustOpened = true;
  $("#pastText").textContent = "Viewing past run · " + fmtWhen(en.started) + " · " + (en.status === "failed" ? "exit " + en.exit_code : en.status) + (en.args ? " · " + en.args : "");
  $("#past").classList.add("show");
  termRender();
});
$("#histClear").onclick = async () => {
  if (!histClearArmed) { histClearArmed = true; $("#histClear").textContent = "Click again to delete saved logs"; return; }
  const r = await itemAction(S.histId, "history-clear");
  histClearArmed = false; $("#histClear").textContent = "Clear history";
  if (r.ok) { if (S.past && S.past.id === S.histId) exitPast(); await loadHistory(); S.last = ""; await refresh(); }
};
$("#histClose").onclick = () => histDlg.close();

// ---------------------------------------------------------------- groups
const groupDlg = $("#groupDlg");
async function groupAction(gid, act) {
  const r = await api("/api/groups/" + encodeURIComponent(gid) + "/" + act, "POST", {});
  if (r.ok) applyListing(r.data); else notify(r.data.error || "Group action failed.");
  setTimeout(refresh, 400);
}
function openGroupDialog(gid) {
  const g = gid ? S.groups.find((x) => x.id === gid) : null;
  S.editGroup = g ? g.id : null;
  $("#groupTitle").textContent = g ? "Edit group" : "New group";
  $("#groupName").value = g ? g.name : "";
  $("#groupDelay").value = g ? g.delay : 1;
  $("#groupDelete").style.visibility = g ? "visible" : "hidden";
  $("#groupMsg").textContent = "";
  $("#groupPickFilter").value = "";
  S.groupOrder = g ? g.items.filter((i) => S.items.some((x) => x.id === i)) : [];
  renderGroupEditor();
  if (!groupDlg.open) groupDlg.showModal();
  $("#groupName").focus();
}
function renderGroupEditor() {
  const byId = Object.fromEntries(S.items.map((i) => [i.id, i]));
  $("#groupOrder").innerHTML = S.groupOrder.length ? S.groupOrder.map((iid, n) =>
    '<div class="order-row"><span class="n">' + (n + 1) + '</span><span class="nm">' + esc(byId[iid] ? byId[iid].name : iid) + "</span>" +
    '<button type="button" class="icon-btn" data-ord="up" data-i="' + n + '" aria-label="Move up"' + (n === 0 ? " disabled" : "") + ">↑</button>" +
    '<button type="button" class="icon-btn" data-ord="down" data-i="' + n + '" aria-label="Move down"' + (n === S.groupOrder.length - 1 ? " disabled" : "") + ">↓</button>" +
    '<button type="button" class="icon-btn" data-ord="rm" data-i="' + n + '" aria-label="Remove">✕</button></div>'
  ).join("") : '<p class="form-note">Tick items below. They start in this order and stop in reverse.</p>';
  const f = $("#groupPickFilter").value.trim().toLowerCase();
  $("#groupPick").innerHTML = S.items.filter((i) => i.startable && (!f || (i.name + " " + i.path).toLowerCase().includes(f))).map((i) =>
    '<label><input type="checkbox" data-gpick="' + esc(i.id) + '"' + (S.groupOrder.includes(i.id) ? " checked" : "") + "> " + esc(i.name) +
    '<span class="pk-path">' + esc(i.path) + "</span></label>"
  ).join("") || '<p class="form-note">No startable items.</p>';
}
$("#groupPickFilter").addEventListener("input", renderGroupEditor);
$("#groupPick").addEventListener("change", (e) => {
  const c = e.target.closest("[data-gpick]");
  if (!c) return;
  const id = c.dataset.gpick;
  if (c.checked) { if (!S.groupOrder.includes(id)) S.groupOrder.push(id); }
  else S.groupOrder = S.groupOrder.filter((x) => x !== id);
  renderGroupEditor();
});
$("#groupOrder").addEventListener("click", (e) => {
  const b = e.target.closest("[data-ord]");
  if (!b) return;
  const i = Number(b.dataset.i), o = S.groupOrder;
  if (b.dataset.ord === "up" && i > 0) [o[i - 1], o[i]] = [o[i], o[i - 1]];
  else if (b.dataset.ord === "down" && i < o.length - 1) [o[i + 1], o[i]] = [o[i], o[i + 1]];
  else if (b.dataset.ord === "rm") o.splice(i, 1);
  renderGroupEditor();
});
$("#groupSave").onclick = async () => {
  const body = { name: $("#groupName").value.trim(), items: S.groupOrder, delay: Number($("#groupDelay").value || 0) };
  if (S.editGroup) body.id = S.editGroup;
  const r = await api("/api/groups", "POST", body);
  if (!r.ok) { $("#groupMsg").textContent = r.data.error || "Could not save."; return; }
  applyListing(r.data); groupDlg.close();
};
let groupDelArmed = false;
$("#groupDelete").onclick = async () => {
  if (!S.editGroup) return;
  if (!groupDelArmed) { groupDelArmed = true; $("#groupDelete").textContent = "Click again to delete"; setTimeout(() => { groupDelArmed = false; $("#groupDelete").textContent = "Delete group"; }, 3000); return; }
  groupDelArmed = false; $("#groupDelete").textContent = "Delete group";
  const r = await api("/api/groups/" + encodeURIComponent(S.editGroup) + "/delete", "POST", {});
  if (r.ok) { applyListing(r.data); groupDlg.close(); }
};
$("#groupClose").onclick = () => groupDlg.close();
$("#newGroupBtn").onclick = () => openGroupDialog(null);

// ---------------------------------------------------------------- notification settings
const notifDlg = $("#notifDlg");
function fillNotifForm() {
  const st = S.settings || {}; const n = st.notify || {};
  $("#nEnabled").checked = !!n.enabled; $("#nAll").checked = !!n.all_items;
  $("#nFinish").checked = !!n.on_finish; $("#nFail").checked = !!n.on_fail; $("#nPrompt").checked = !!n.on_prompt;
  $("#nMin").value = n.min_seconds != null ? n.min_seconds : 10;
  $("#nBrowser").checked = !!n.browser; $("#nMac").checked = !!n.macos;
  const perm = typeof Notification === "undefined" ? "not supported in this browser" : Notification.permission === "granted" ? "allowed" : Notification.permission === "denied" ? "blocked in browser settings" : "will ask for permission";
  $("#nBrowserState").textContent = "(" + perm + ")";
  $("#nMacState").textContent = st.macos_notifications ? "(available)" : "(only on macOS; not available here)";
  $("#notifMsg").textContent = "";
}
$("#notifBtn").onclick = async () => { await loadSettings(); fillNotifForm(); notifDlg.showModal(); };
$("#notifClose").onclick = () => notifDlg.close();
async function saveNotif() {
  const n = {
    enabled: $("#nEnabled").checked, all_items: $("#nAll").checked, on_finish: $("#nFinish").checked,
    on_fail: $("#nFail").checked, on_prompt: $("#nPrompt").checked, min_seconds: Number($("#nMin").value || 0),
    browser: $("#nBrowser").checked, macos: $("#nMac").checked,
  };
  if (n.enabled && n.browser && typeof Notification !== "undefined" && Notification.permission === "default") {
    try { await Notification.requestPermission(); } catch (e) {}
  }
  const r = await api("/api/settings", "POST", { notify: n });
  if (!r.ok) { $("#notifMsg").textContent = r.data.error || "Could not save."; return false; }
  S.settings = r.data; renderNotifBtn(); S.last = ""; await refresh(); fillNotifForm();
  return true;
}
$("#notifSave").onclick = async () => { if (await saveNotif()) notifDlg.close(); };
$("#notifTest").onclick = async () => {
  if (!(await saveNotif())) return;
  const r = await api("/api/notify-test", "POST", { macos: $("#nMac").checked });
  if (r.ok) {
    S.evSeq = Math.max(S.evSeq, r.data.event.seq);
    showEvent(r.data.event);
    $("#notifMsg").textContent = "Test sent" + (r.data.event.macos ? " (also as a macOS notification)" : "") + ".";
  }
};

// ---------------------------------------------------------------- AI: model servers + running agents
// Polls /api/ai every 10 s, only while this tab is visible. The server only talks to localhost
// and only lists agent processes (it never reads their files or transcripts).
const AI = { data: null, open: localStorage.getItem("launcher.aiOpen") === "1", busy: false, modelsOpen: {}, flash: null };
async function pollAI(force) {
  if (document.visibilityState !== "visible" || AI.busy) return;
  AI.busy = true;
  try {
    const r = await api("/api/ai" + (force ? "?force=1" : ""));
    if (r.ok) { AI.data = r.data; renderAI(); }
  } finally { AI.busy = false; }
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") pollAI(); });
setInterval(() => pollAI(false), 10000);

function aiCounts() {
  const d = AI.data || { servers: [], agents: [] };
  const live = d.servers.filter((s) => s.running);
  const agents = d.agents.filter((a) => !a.model);
  return { live, agents, d };
}
function fmtCtx(n) { return n >= 1024 ? Math.round(n / 1024) + "k ctx" : n + " ctx"; }
function modelRow(m) {
  const meta = [m.params, m.quant, m.vram ? fmtBytes(m.vram) + " VRAM" : (m.size ? fmtBytes(m.size) : null), m.ctx ? fmtCtx(m.ctx) : null, m.type && m.type !== "llm" ? m.type : null].filter(Boolean).join(" · ");
  const state = m.loaded === true ? "loaded" : m.loaded === false ? "available" : "";
  return '<div class="ai-model' + (m.loaded ? " loaded" : "") + '">' +
    (m.loaded ? '<span class="dot" aria-hidden="true"></span>' : '<span class="dot off" aria-hidden="true"></span>') +
    '<span class="m-text"><span class="m-name" title="' + esc(m.name) + '">' + esc(m.name) + "</span>" +
    (meta ? '<span class="m-meta">' + esc(meta) + "</span>" : "") + "</span>" +
    (state ? '<span class="m-state">' + state + "</span>" : "") + "</div>";
}
function serverCard(s) {
  if (!s.running) {
    return '<div class="ai-card off" data-ai-server="' + esc(s.id) + '"><div class="ai-top"><div class="ai-name"><span class="dot off" aria-hidden="true"></span>' + esc(s.name) + '</div><span class="badge">' + (s.starting ? "starting" : "not running") + "</span></div>" +
      '<div class="mono ai-empty">' + esc(s.start_cmd || "") + " · port " + s.port + "</div>" +
      '<div class="ai-actions">' + (s.run_id ? '<button type="button" class="btn xs" data-ai-output="' + esc(s.run_id) + '" data-ai-name="' + esc(s.name) + '">Output</button>' : "") +
      (s.can_start ? '<button type="button" class="btn xs primary" data-ai-start="' + esc(s.key) + '">' + PLAY + " Start</button>" : "") + "</div></div>";
  }
  const loaded = s.models.filter((m) => m.loaded);
  const showAll = !!AI.modelsOpen[s.id];
  const list = showAll ? s.models : s.models.slice(0, 6);
  const h = s.health;
  const stats = [
    s.version ? "v" + s.version : null,
    s.pid ? "PID " + s.pid : null,
    h ? "CPU " + h.cpu.toFixed(1) + "%" : null,
    h ? "MEM " + fmtBytes(h.rss) : null,
    h && h.started_at ? "up " + fmtDur(Date.now() / 1000 - h.started_at) : null,
  ].filter(Boolean).join(" · ");
  const count = s.models.length ? (loaded.length + " loaded · " + s.models.length + " model" + (s.models.length === 1 ? "" : "s")) : "";
  let html = '<div class="ai-card" data-ai-server="' + esc(s.id) + '"><div class="ai-top"><div class="ai-name"><span class="dot" aria-hidden="true"></span>' + esc(s.name) +
    (s.ours ? ' <span class="badge">via Slothmore</span>' : "") + '</div><span class="badge">' + (s.ui ? "UI" : "API") + " :" + s.port + "</span></div>" +
    '<a class="ai-url mono" href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.url.replace(/^https?:\/\//, "")) + "</a>" +
    (stats ? '<div class="health">' + esc(stats) + "</div>" : "") +
    (s.memory ? '<div class="health"><span>' + esc(s.memory_label || "Memory") + " <b>" + fmtBytes(s.memory) + "</b></span></div>" : "");
  if (s.models.length) {
    html += '<div class="form-note">' + esc(count) + '</div><div class="ai-models">' + list.map(modelRow).join("") + "</div>";
    if (s.models.length > 6) html += '<button type="button" class="pill-btn" data-ai-more="' + esc(s.id) + '">' + (showAll ? "Show fewer" : "Show all " + s.models.length) + "</button>";
  } else {
    html += '<div class="ai-empty">' + esc(s.hint || (s.ui ? "Web UI. Open it to pick a model." : "No models reported.")) + "</div>";
  }
  html += '<div class="ai-actions">' +
    (s.run_id ? '<button type="button" class="btn xs" data-ai-output="' + esc(s.run_id) + '" data-ai-name="' + esc(s.name) + '">Output</button>' : "") +
    '<a class="btn xs" href="' + esc(s.url) + '" target="_blank" rel="noopener">Open ' + OPEN + "</a>" +
    (s.can_stop ? '<button type="button" class="btn xs" data-ai-stop="' + esc(s.key) + '" data-port="' + s.port + '">' + STOP + " Stop</button>"
                : (s.stop_note ? '<span class="form-note" title="' + esc(s.stop_note) + '">' + esc(s.stop_note) + "</span>" : "")) +
    "</div></div>";
  return html;
}
function agentRow(a) {
  const stats = '<span>CPU <b>' + (a.cpu != null ? a.cpu.toFixed(1) : "–") + '%</b></span><span>MEM <b>' + fmtBytes(a.rss) + "</b></span>" +
    (a.started_at ? '<span>UP <b data-since="' + a.started_at + '">' + fmtDur(Date.now() / 1000 - a.started_at) + "</b></span>" : "") +
    (a.children ? "<span>" + a.children + " child proc" + (a.children === 1 ? "" : "s") + "</span>" : "");
  const link = a.item_id ? '<button type="button" class="a-link" data-ai-reveal="' + esc(a.item_id) + '" title="Show the card">→ ' + esc(a.item_name || "card") + "</button>" : "";
  return '<div class="ai-agent' + (AI.flash === a.pid ? " flash" : "") + '" data-agent-pid="' + a.pid + '"><div class="a-main"><span class="dot" aria-hidden="true"></span><div class="a-text">' +
    '<div class="a-name">' + esc(a.name) + ' <span class="badge">PID ' + a.pid + "</span>" + (a.launched_by ? ' <span class="badge">via Slothmore</span>' : "") + link + "</div>" +
    '<div class="a-cwd mono" title="' + esc(a.cwd || "") + '">' + esc(a.cwd || "working folder unknown") + "</div>" +
    '<div class="a-stats mono">' + stats + "</div></div></div>" +
    '<div class="a-actions">' + (a.item_id ? '<button type="button" class="btn xs" data-ai-reveal="' + esc(a.item_id) + '">Reveal folder</button>' : (a.cwd ? '<button type="button" class="btn xs" data-ai-copy="' + esc(a.cwd) + '">Copy path</button>' : "")) +
    '<button type="button" class="btn xs" data-agent-stop="' + a.pid + '" data-agent-name="' + esc(a.name) + '">' + STOP + " Stop</button></div></div>";
}
function renderAI() {
  const { live, agents, d } = aiCounts();
  const runners = d.agents.filter((a) => a.model);
  const nav = $("#aiNav");
  if (nav) {
    const sSub = live.length ? live.map((s) => s.name).join(" · ") : (d.servers.length ? "Installed, not running" : "None found on localhost");
    const aSub = agents.length ? Array.from(new Set(agents.map((a) => a.name))).join(" · ") : "No coding agents running";
    nav.innerHTML = '<li><button type="button" data-ai-open="models" aria-expanded="' + AI.open + '"><span><span>Models</span><span class="sub">' + esc(sSub) + '</span></span><span class="count' + (live.length ? " live" : "") + '">' + live.length + "</span></button></li>" +
      '<li><button type="button" data-ai-open="agents" aria-expanded="' + AI.open + '"><span><span>Agents</span><span class="sub">' + esc(aSub) + '</span></span><span class="count' + (agents.length ? " live" : "") + '">' + agents.length + "</span></button></li>";
  }
  const panel = $("#aiPanel");
  panel.hidden = !AI.open;
  if (!AI.open) return;
  const settings = d.settings || {};
  let html = '<div class="ai-head"><div><h2>AI on this machine</h2><p class="ai-privacy">Local only: probes localhost ports, lists processes. No prompts, files or transcripts are read.</p></div>' +
    '<div class="ai-tools"><button type="button" class="btn sm" id="aiRefresh">Refresh</button><button type="button" class="btn sm" id="aiSettingsBtn">Ports…</button><button type="button" class="btn sm ghost" id="aiHide">Hide</button></div></div>';
  html += '<div class="ai-sub" id="aiModels"><h3 class="eyebrow">Local model servers</h3>';
  if (settings.enabled === false) html += '<p class="ai-empty">Model server detection is off (Ports… to turn it on).</p>';
  else if (!d.servers.length) html += '<p class="ai-empty">Nothing found on the usual ports (Ollama 11434, LM Studio 1234, vLLM 8000, llama.cpp 8080, Jan 1337, …). Change ports under Ports….</p>';
  else html += '<div class="ai-grid">' + d.servers.map(serverCard).join("") + "</div>";
  html += "</div>";
  html += '<div class="ai-sub" id="aiAgents"><h3 class="eyebrow">Running agents</h3>';
  if (settings.agents === false) html += '<p class="ai-empty">Agent detection is off.</p>';
  else if (!agents.length) html += '<p class="ai-empty">No Claude Code, Codex, Aider, Gemini CLI, Cursor Agent, Goose, OpenHands or Cline processes right now.</p>';
  else html += '<div class="ai-agents">' + agents.map(agentRow).join("") + "</div>";
  if (runners.length) html += '<p class="form-note">' + runners.length + " Ollama runner process" + (runners.length === 1 ? "" : "es") + " · " + runners.map((r) => "PID " + r.pid + " " + fmtBytes(r.rss)).join(", ") + "</p>";
  html += "</div>";
  const ae = document.activeElement;
  const keep = ae && panel.contains(ae) && ae.id ? ae.id : null;
  panel.innerHTML = html;
  if (keep && document.getElementById(keep)) document.getElementById(keep).focus();
  if (palDlg.open) renderPalette();
}
function openAI(which) {
  AI.open = true; localStorage.setItem("launcher.aiOpen", "1");
  renderAI();
  const target = which === "agents" ? $("#aiAgents") : $("#aiPanel");
  if (target && target.scrollIntoView) target.scrollIntoView({ behavior: "smooth", block: "start" });
  pollAI(true);
}
function revealItem(id) {
  const it = S.items.find((x) => x.id === id);
  if (!it) { notify("That folder isn't on the dashboard any more."); return; }
  if (S.source !== "all" && S.source !== it.source && S.source !== it.root) S.source = "all";
  if (S.q) { S.q = ""; $("#q").value = ""; }
  S.kind = "all";
  select(id);
  requestAnimationFrame(() => {
    const el = document.querySelector('.card[data-id="' + CSS.escape(id) + '"]');
    if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.focus && el.focus(); }
  });
}
function showAiOutput(runId, name) {
  S.sel = null; S.aiRun = { id: runId, name: name };
  if (S.past) { S.past = null; $("#past").classList.remove("show"); }
  openStream(runId, 0);
  render(); renderTermHeader();
  $("#term").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
// Confirm dialog (stopping a process Slothmore didn't start always goes through here).
const confirmDlg = $("#confirmDlg");
function askConfirm(title, body, okLabel) {
  return new Promise((resolve) => {
    $("#confirmTitle").textContent = title;
    $("#confirmBody").textContent = body;
    $("#confirmOk").textContent = okLabel || "Stop";
    const done = (v) => { confirmDlg.close(); resolve(v); };
    $("#confirmOk").onclick = () => done(true);
    $("#confirmCancel").onclick = () => done(false);
    confirmDlg.oncancel = (e) => { e.preventDefault(); done(false); };
    confirmDlg.showModal();
    $("#confirmCancel").focus();
  });
}
async function aiStartServer(key) {
  const r = await api("/api/ai/servers/" + encodeURIComponent(key) + "/start", "POST", {});
  if (!r.ok) { notify(r.data.error || "Could not start."); return; }
  if (r.data.ai) { AI.data = r.data.ai; renderAI(); }
  const s = (AI.data.servers || []).find((x) => x.key === key);
  showAiOutput(r.data.run_id, s ? s.name : key);
  setTimeout(() => pollAI(true), 1500); setTimeout(() => pollAI(true), 4000);
}
async function aiStopServer(key, port) {
  const s = ((AI.data && AI.data.servers) || []).find((x) => x.key === key && x.port === port);
  const name = s ? s.name : key;
  let body = { port: port };
  if (!(s && s.ours)) {
    const how = s && s.stop_how === "cli" ? "Slothmore will run its CLI stop command (" + (key === "lmstudio" ? "lms server stop" : "stop") + ")."
      : "Slothmore will send SIGTERM to PID " + (s && s.pid ? s.pid : "?") + (s && s.command ? " (" + s.command + ")" : "") + ".";
    const extra = key === "ollama" ? "\nIf the Ollama menu bar app is running, it may start the server again; quit the app instead." : "";
    const ok = await askConfirm("Stop " + name + "?", name + " on port " + port + " wasn't started by Slothmore. " + how + extra, "Stop " + name);
    if (!ok) return;
    body.confirm = true;
  }
  const r = await api("/api/ai/servers/" + encodeURIComponent(key) + "/stop", "POST", body);
  if (!r.ok) { notify(r.data.error === "confirm_required" ? "Not stopped." : (r.data.error || "Could not stop.")); return; }
  if (r.data.ai) { AI.data = r.data.ai; renderAI(); }
  setTimeout(() => pollAI(true), 1500);
}
async function aiStopAgent(pid, name) {
  const a = ((AI.data && AI.data.agents) || []).find((x) => x.pid === pid);
  const where = a && a.cwd ? " in " + a.cwd : "";
  const ok = await askConfirm("Stop " + name + "?", name + " (PID " + pid + ")" + where + " is not a Slothmore process.\nSlothmore sends Ctrl+C (SIGINT) first and SIGTERM after 3 seconds if it is still running. Unsaved work in that session may be lost.", "Stop " + name);
  if (!ok) return;
  const r = await api("/api/ai/agents/" + pid + "/stop", "POST", { confirm: true });
  if (!r.ok) { notify(r.data.error || "Could not stop."); return; }
  setTimeout(() => pollAI(true), 800); setTimeout(() => pollAI(true), 3800);
}
document.addEventListener("click", (e) => {
  const t = e.target;
  const o = t.closest("[data-ai-open]");
  if (o) {
    const w = o.dataset.aiOpen;
    if (AI.open && AI.lastOpen === w && o.closest("#aiNav")) { AI.open = false; AI.lastOpen = null; localStorage.setItem("launcher.aiOpen", "0"); renderAI(); }
    else { AI.lastOpen = w; openAI(w); }
    return;
  }
  if (!t.closest("#aiPanel")) return;
  if (t.closest("#aiRefresh")) { pollAI(true); return; }
  if (t.closest("#aiHide")) { AI.open = false; AI.lastOpen = null; localStorage.setItem("launcher.aiOpen", "0"); renderAI(); return; }
  if (t.closest("#aiSettingsBtn")) { openAiSettings(); return; }
  const st = t.closest("[data-ai-start]"); if (st) { aiStartServer(st.dataset.aiStart); return; }
  const sp = t.closest("[data-ai-stop]"); if (sp) { aiStopServer(sp.dataset.aiStop, Number(sp.dataset.port)); return; }
  const out = t.closest("[data-ai-output]"); if (out) { showAiOutput(out.dataset.aiOutput, out.dataset.aiName); return; }
  const more = t.closest("[data-ai-more]"); if (more) { AI.modelsOpen[more.dataset.aiMore] = !AI.modelsOpen[more.dataset.aiMore]; renderAI(); return; }
  const rv = t.closest("[data-ai-reveal]"); if (rv) { revealItem(rv.dataset.aiReveal); return; }
  const cp = t.closest("[data-ai-copy]"); if (cp) { copyText(cp.dataset.aiCopy); return; }
  const ag = t.closest("[data-agent-stop]"); if (ag) { aiStopAgent(Number(ag.dataset.agentStop), ag.dataset.agentName); return; }
});
// Agent chips on cards: capture phase so the card's own click (select) doesn't run.
$("#sections").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-agent-chip]");
  if (!chip) return;
  e.stopPropagation(); e.preventDefault();
  AI.flash = Number(chip.dataset.agentChip);
  openAI("agents");
  setTimeout(() => { AI.flash = null; renderAI(); }, 2400);
}, true);
// AI settings (ports)
const aiDlg = $("#aiDlg");
function openAiSettings() {
  const s = (AI.data && AI.data.settings) || { servers: [], enabled: true, agents: true };
  $("#aiEnabled").checked = s.enabled !== false;
  $("#aiAgentsOn").checked = s.agents !== false;
  $("#aiPorts").innerHTML = s.servers.map((x) => '<div class="ai-port-row"><label for="aiport-' + esc(x.key) + '">' + esc(x.name) +
    ' <span class="form-note">default ' + esc(x.default.join(", ")) + '</span></label><input class="mono" id="aiport-' + esc(x.key) + '" data-ai-port="' + esc(x.key) + '" value="' + esc(x.ports.join(", ")) + '" inputmode="numeric" spellcheck="false" autocomplete="off"></div>').join("");
  $("#aiMsg").textContent = "";
  aiDlg.showModal();
}
$("#aiClose").onclick = () => aiDlg.close();
$("#aiReset").onclick = () => {
  const s = (AI.data && AI.data.settings) || { servers: [] };
  s.servers.forEach((x) => { const el = document.getElementById("aiport-" + x.key); if (el) el.value = x.default.join(", "); });
};
$("#aiSave").onclick = async () => {
  const ports = {};
  document.querySelectorAll("[data-ai-port]").forEach((el) => { ports[el.dataset.aiPort] = el.value; });
  const r = await api("/api/ai/settings", "POST", { enabled: $("#aiEnabled").checked, agents: $("#aiAgentsOn").checked, ports: ports });
  if (!r.ok) { $("#aiMsg").textContent = r.data.error || "Could not save."; return; }
  aiDlg.close();
  pollAI(true);
};
pollAI(true);

// ---------------------------------------------------------------- ports
const Ports = { open: false, data: null, timer: null, q: "" };

function portsList() {
  return (Ports.data && Ports.data.ports) || [];
}

function startPortsTimer() {
  if (Ports.timer) return;
  Ports.timer = setInterval(() => { if (document.visibilityState !== "hidden") loadPorts(); }, Ports.open ? 5000 : 15000);
}
function resetPortsTimer() {
  if (Ports.timer) { clearInterval(Ports.timer); Ports.timer = null; }
  startPortsTimer();
}

async function loadPorts() {
  const r = await api("/api/ports");
  if (r.ok) { Ports.data = r.data; renderPorts(); }
  else if (Ports.open) notify((r.data && r.data.error) || "Could not list ports.");
}

function renderPorts() {
  const list = portsList();
  const count = $("#portsCount");
  const btn = $("#portsBtn");
  if (count) count.textContent = list.length;
  if (btn) btn.setAttribute("aria-expanded", Ports.open ? "true" : "false");
  const panel = $("#portsPanel");
  if (!panel) return;
  panel.hidden = !Ports.open;
  if (!Ports.open) return;
  const q = (Ports.q || "").trim().toLowerCase();
  const rows = list.filter((p) => {
    if (!q) return true;
    const hay = [p.port, p.address, (p.addresses || []).join(" "), p.command, p.pid, p.user, p.folder, p.item_name, p.ours ? "slothmore" : ""].join(" ").toLowerCase();
    return hay.indexOf(q) !== -1;
  });
  let html = '<div class="ports-head"><div><h2>Listening ports</h2><p class="ports-note">TCP sockets in LISTEN on this machine. Stop only appears for processes you own, and Slothmore checks again just before signalling.</p></div>';
  html += '<div class="ports-tools"><label class="vh" for="portsFilter">Filter ports</label><input type="search" id="portsFilter" placeholder="Filter by port, process, user…" value="' + esc(Ports.q) + '" autocomplete="off" spellcheck="false">';
  html += '<button type="button" class="btn sm" id="portsRefresh">Refresh</button>';
  html += '<button type="button" class="btn sm" id="portsHide">Hide</button></div></div>';
  if (!rows.length) {
    html += '<p class="ports-empty">' + (list.length ? "Nothing matches that filter." : "No listening TCP ports were found.") + "</p>";
  } else {
    html += '<div class="ports-table-wrap"><table class="ports-table"><thead><tr><th>Port</th><th>Address</th><th>Process</th><th>PID</th><th>User</th><th>Project</th><th></th></tr></thead><tbody>';
    rows.forEach((p) => {
      const addrs = (p.addresses || [p.address]).map((a) => a === "*" ? "0.0.0.0" : a).join(", ");
      const folder = p.item_name ? (p.item_name + (p.folder ? " · " + p.folder : "")) : (p.folder || "");
      const ours = p.ours ? ' <span class="badge">Slothmore</span>' : "";
      html += '<tr class="' + (p.ours ? "ours" : "") + '">';
      html += '<td class="p-port">' + esc(String(p.port)) + ours + "</td>";
      html += '<td class="p-addr">' + esc(addrs) + "</td>";
      html += '<td class="p-cmd" title="' + esc(p.command || "") + '">' + esc(p.command || "—") + "</td>";
      html += '<td class="p-pid">' + (p.pid != null ? esc(String(p.pid)) : "—") + "</td>";
      html += '<td class="p-user">' + esc(p.user || "—") + "</td>";
      html += '<td class="p-folder" title="' + esc(folder) + '">' + esc(folder || "—") + "</td>";
      html += '<td><div class="p-acts">';
      if (p.http && p.url) html += '<a class="p-open" href="' + esc(p.url) + '" target="_blank" rel="noopener">Open</a>';
      if (p.can_stop) html += '<button type="button" class="btn sm danger" data-port-stop="' + esc(String(p.port)) + '" data-pid="' + esc(String(p.pid)) + '" data-cmd="' + esc(p.command || "") + '">Stop</button>';
      html += "</div></td></tr>";
    });
    html += "</tbody></table></div>";
  }
  panel.innerHTML = html;
  const filt = $("#portsFilter");
  if (filt) {
    filt.oninput = () => { Ports.q = filt.value; renderPorts(); const f = $("#portsFilter"); if (f) { f.focus(); const n = Ports.q.length; f.setSelectionRange(n, n); } };
  }
}

function openPorts() {
  Ports.open = true;
  localStorage.setItem("launcher.portsOpen", "1");
  resetPortsTimer();
  loadPorts();
  const panel = $("#portsPanel");
  if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function hidePorts() {
  Ports.open = false;
  localStorage.setItem("launcher.portsOpen", "0");
  renderPorts();
  resetPortsTimer();
}

async function stopPort(port, pid, cmd) {
  const label = (cmd || "process") + " (PID " + pid + ") on port " + port;
  const ok = await askConfirm("Stop " + (cmd || "process") + "?", "Slothmore will send SIGTERM to " + label + " after checking that the same process is still listening. Unsaved work may be lost.", "Stop");
  if (!ok) return;
  const r = await api("/api/ports/stop", "POST", { port: Number(port), pid: Number(pid), confirm: true });
  if (!r.ok) { notify((r.data && r.data.error) || "Could not stop."); loadPorts(); return; }
  notify("Stopped " + label + ".");
  setTimeout(loadPorts, 400);
  setTimeout(loadPorts, 2000);
}

document.addEventListener("click", (e) => {
  const t = e.target;
  if (t.closest("[data-ports-open]")) {
    if (Ports.open) hidePorts(); else openPorts();
    return;
  }
  if (!t.closest("#portsPanel")) return;
  if (t.closest("#portsRefresh")) { loadPorts(); return; }
  if (t.closest("#portsHide")) { hidePorts(); return; }
  const st = t.closest("[data-port-stop]");
  if (st) { stopPort(st.dataset.portStop, st.dataset.pid, st.dataset.cmd); }
});

// ---------------------------------------------------------------- boot
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
loadSettings();
refresh();
setInterval(refresh, 1500);
if (localStorage.getItem("launcher.portsOpen") === "1") { Ports.open = true; }
startPortsTimer();
loadPorts();

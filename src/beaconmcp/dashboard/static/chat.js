// BeaconMCP dashboard chat client.
// Vanilla ES module. Drives the design-system UI:
// sidebar + model popover + tool cards + thinking + confirm + usage.

const root = document.querySelector(".chat-root");
if (!root) throw new Error("chat-root missing");

const CLIENT_ID = root.dataset.clientId;
const CLIENT_NAME = root.dataset.clientName;
const DEFAULT_MODEL = root.dataset.defaultModel;
const DEFAULT_EFFORT = root.dataset.defaultEffort;
const VALID_MODELS = JSON.parse(root.dataset.validModels || "[]");
const VALID_EFFORTS = JSON.parse(root.dataset.validEfforts || "[]");

// --- Model catalog (mirrors src/beaconmcp/dashboard/conversations.py) ---
const MODEL_CATALOG = {
  "gemini-2.5-flash": {
    shortName: "2.5 Flash", name: "Gemini 2.5 Flash", group: "Gemini 2", preview: false,
  },
  "gemini-2.5-pro": {
    shortName: "2.5 Pro", name: "Gemini 2.5 Pro", group: "Gemini 2", preview: false,
  },
  "gemini-3-flash-preview": {
    shortName: "3 Flash", name: "Gemini 3 Flash", group: "Gemini 3", preview: true,
  },
  "gemini-3.1-pro-preview": {
    shortName: "3.1 Pro", name: "Gemini 3.1 Pro", group: "Gemini 3", preview: true,
  },
};

const csrfToken = () =>
  document.querySelector('meta[name="csrf-token"]')?.content ||
  getCookie("beaconmcp_csrf_token") || "";

function getCookie(name) {
  return document.cookie
    .split(";")
    .map((s) => s.trim().split("="))
    .filter(([k]) => k === name)
    .map(([, v]) => decodeURIComponent(v || ""))[0];
}

// ---------------- Markdown ----------------
// Kept from the previous dashboard: extracts fences first, then runs
// line-level block detection and inline formatting on the rest.

const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}

function _extractFences(raw) {
  const blocks = [];
  const stripped = raw.replace(
    /```([a-zA-Z0-9+_.-]*)\n([\s\S]*?)```/g,
    (_, lang, body) => {
      const idx = blocks.length;
      blocks.push({ lang, body: body.replace(/\n+$/, "") });
      return `\u0000CODEBLOCK${idx}\u0000`;
    },
  );
  return { stripped, blocks };
}

function _renderInline(text) {
  let out = text.replace(/`([^`\n]+?)`/g, (_, c) => `<code>${c}</code>`);
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)/g, "<em>$1</em>");
  out = out.replace(/~~([^~\n]+?)~~/g, "<s>$1</s>");
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, label, href) =>
      `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`,
  );
  return out;
}

function _isBulletLine(line) { return /^\s*[-*+]\s+/.test(line); }
function _isOrderedLine(line) { return /^\s*\d+\.\s+/.test(line); }
function _stripBullet(line) { return line.replace(/^\s*[-*+]\s+/, ""); }
function _stripOrdered(line) { return line.replace(/^\s*\d+\.\s+/, ""); }

function _splitTableRow(line) {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells = [];
  let buf = "";
  for (let k = 0; k < trimmed.length; k += 1) {
    const ch = trimmed[k];
    if (ch === "\\" && trimmed[k + 1] === "|") {
      buf += "|"; k += 1; continue;
    }
    if (ch === "|") { cells.push(buf.trim()); buf = ""; continue; }
    buf += ch;
  }
  cells.push(buf.trim());
  return cells;
}

function _isTableSeparator(line) {
  if (!line || !/\|/.test(line)) return false;
  const cells = _splitTableRow(line);
  if (cells.length === 0) return false;
  return cells.every((c) => /^:?-{3,}:?$/.test(c));
}

function _tableAlignments(sepLine) {
  return _splitTableRow(sepLine).map((c) => {
    const left = c.startsWith(":");
    const right = c.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return null;
  });
}

function renderMarkdown(text) {
  const { stripped, blocks } = _extractFences(text);
  const lines = stripped.split("\n");
  const out = [];
  let i = 0;
  let inParagraph = [];

  const flushParagraph = () => {
    if (inParagraph.length === 0) return;
    const body = inParagraph.map((l) => _renderInline(escapeHtml(l))).join("<br>");
    out.push(`<p>${body}</p>`);
    inParagraph = [];
  };

  while (i < lines.length) {
    const line = lines[i];

    if (/^\s*$/.test(line)) { flushParagraph(); i += 1; continue; }

    // Heading
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      flushParagraph();
      const level = h[1].length;
      out.push(`<h${level}>${_renderInline(escapeHtml(h[2]))}</h${level}>`);
      i += 1; continue;
    }

    // HR
    if (/^\s*([-*_])\s*\1\s*\1[-*_\s]*$/.test(line)) {
      flushParagraph();
      out.push("<hr>");
      i += 1; continue;
    }

    // Blockquote
    if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      const body = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      out.push(`<blockquote>${body.map((l) => _renderInline(escapeHtml(l))).join("<br>")}</blockquote>`);
      continue;
    }

    // Table
    if (/\|/.test(line) && i + 1 < lines.length && _isTableSeparator(lines[i + 1])) {
      flushParagraph();
      const headers = _splitTableRow(line);
      const aligns = _tableAlignments(lines[i + 1]);
      i += 2;
      const rows = [];
      while (i < lines.length && /\|/.test(lines[i]) && !/^\s*$/.test(lines[i])) {
        rows.push(_splitTableRow(lines[i]));
        i += 1;
      }
      const th = headers
        .map((c, idx) => {
          const a = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
          return `<th${a}>${_renderInline(escapeHtml(c))}</th>`;
        })
        .join("");
      const trs = rows
        .map((r) => {
          const tds = r
            .map((c, idx) => {
              const a = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
              return `<td${a}>${_renderInline(escapeHtml(c))}</td>`;
            })
            .join("");
          return `<tr>${tds}</tr>`;
        })
        .join("");
      out.push(`<table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`);
      continue;
    }

    // Lists
    if (_isBulletLine(line) || _isOrderedLine(line)) {
      flushParagraph();
      const ordered = _isOrderedLine(line);
      const strip = ordered ? _stripOrdered : _stripBullet;
      const pred = ordered ? _isOrderedLine : _isBulletLine;
      const items = [];
      while (i < lines.length && pred(lines[i])) {
        items.push(_renderInline(escapeHtml(strip(lines[i]))));
        i += 1;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map((it) => `<li>${it}</li>`).join("")}</${tag}>`);
      continue;
    }

    inParagraph.push(line);
    i += 1;
  }
  flushParagraph();

  let html = out.join("");
  html = html.replace(/\u0000CODEBLOCK(\d+)\u0000/g, (_, idx) => {
    const { lang, body } = blocks[Number(idx)];
    const cls = lang ? ` class="lang-${escapeHtml(lang)}"` : "";
    return `<pre><code${cls}>${escapeHtml(body)}</code></pre>`;
  });
  html = html.replace(/<p>(<pre>[\s\S]*?<\/pre>)<\/p>/g, "$1");
  return html;
}

// ---------------- DOM helpers ----------------

function h(tag, props = {}, children = []) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === "dataset") {
      for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
    } else if (k === "style" && typeof v === "object") {
      Object.assign(el.style, v);
    } else if (v === true) {
      el.setAttribute(k, "");
    } else {
      el.setAttribute(k, v);
    }
  }
  for (const child of [].concat(children)) {
    if (child == null || child === false) continue;
    el.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return el;
}

function icon(name, size = 14, cls = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  if (cls) svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

// ---------------- API helper ----------------

async function apiJson(url, { method = "GET", body = null } = {}) {
  const headers = { "Accept": "application/json" };
  if (body !== null) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  const res = await fetch(url, {
    method,
    headers,
    body: body === null ? null : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (res.status === 401) {
    const data = await res.json().catch(() => ({}));
    if (data.error === "bearer_expired") {
      window.location.href = "/app/refresh?next=/app/chat";
    } else {
      window.location.href = "/app/login";
    }
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error(`${method} ${url} → ${res.status}`);
  if (res.status === 204) return null;
  return res.json();
}

// ---------------- State ----------------

const state = {
  conversations: [],
  active: null,
  messages: [],
  streaming: false,
  abortController: null,
  model: DEFAULT_MODEL,
  effort: DEFAULT_EFFORT,
};

const el = {
  sidebar: document.getElementById("sidebar"),
  openSidebar: document.getElementById("open-sidebar"),
  closeSidebar: document.getElementById("close-sidebar"),
  sidebarBackdrop: document.getElementById("sidebar-backdrop"),
  newChat: document.getElementById("new-chat"),
  convList: document.getElementById("conv-list"),
  convTitle: document.getElementById("conv-title"),
  convMenu: document.getElementById("conv-menu"),
  messages: document.getElementById("messages"),
  composer: document.getElementById("composer-input"),
  send: document.getElementById("composer-send"),
  modelPicker: document.getElementById("model-picker"),
  modelChip: document.getElementById("model-chip"),
  modelChipName: document.getElementById("model-chip-name"),
  composerHint: document.getElementById("composer-hint"),
  composerHintText: document.getElementById("composer-hint-text"),
  usage5h: document.getElementById("usage-5h"),
  usage5hValue: document.getElementById("usage-5h-value"),
  usageWeek: document.getElementById("usage-week"),
  usageWeekValue: document.getElementById("usage-week-value"),
  usageBackdrop: document.getElementById("usage-backdrop"),
  usageModal: document.getElementById("usage-modal"),
  usageModalClose: document.getElementById("usage-modal-close"),
  usageModal5hPct: document.getElementById("usage-modal-5h-pct"),
  usageModal5hReset: document.getElementById("usage-modal-5h-reset"),
  usageModal5hFill: document.getElementById("usage-modal-5h-fill"),
  usageSection5h: document.getElementById("usage-section-5h"),
  usageModalWeekPct: document.getElementById("usage-modal-week-pct"),
  usageModalWeekFill: document.getElementById("usage-modal-week-fill"),
  usageSectionWeek: document.getElementById("usage-section-week"),
  usageModalUpdated: document.getElementById("usage-modal-updated"),
  usageModalRefresh: document.getElementById("usage-modal-refresh"),
  clientAvatar: document.getElementById("client-avatar"),
};

// Client avatar: two-letter initials from CLIENT_NAME
(function initAvatar() {
  const parts = (CLIENT_NAME || "?").trim().split(/\s+/).slice(0, 2);
  const initials = parts.map((p) => p[0] || "").join("").toUpperCase() || "?";
  el.clientAvatar.textContent = initials.slice(0, 2);
})();

// ---------------- Sidebar ----------------

function openSidebar() {
  el.sidebar.classList.add("open");
  el.sidebarBackdrop.hidden = false;
}
function closeSidebar() {
  el.sidebar.classList.remove("open");
  el.sidebarBackdrop.hidden = true;
}
el.openSidebar?.addEventListener("click", openSidebar);
el.closeSidebar?.addEventListener("click", closeSidebar);
el.sidebarBackdrop?.addEventListener("click", closeSidebar);

// ---------------- Conversation list ----------------

function renderConvList() {
  el.convList.innerHTML = "";
  for (const c of state.conversations) {
    const item = h("div", {
      class: "conv-item" + (state.active?.id === c.id ? " active" : ""),
      dataset: { id: c.id },
      onClick: (e) => {
        if (e.target.closest(".conv-menu-btn")) return;
        loadConversation(c.id);
        closeSidebar();
      },
    }, [
      h("span", { class: "conv-title", text: c.title || "New chat" }),
      h("button", {
        class: "conv-menu-btn",
        "aria-label": "Options",
        onClick: (e) => {
          e.preventDefault();
          e.stopPropagation();
          openConvMenu(c, e.currentTarget);
        },
      }, [icon("more", 14)]),
    ]);
    el.convList.append(item);
  }
}

function openConvMenu(conv, anchor) {
  document.querySelector(".conv-popover")?.remove();
  const rect = anchor.getBoundingClientRect();
  const pop = h("div", { class: "conv-popover" });
  pop.style.top = `${rect.bottom + 4}px`;
  pop.style.left = `${Math.max(8, rect.right - 170)}px`;

  const rename = h("button", {
    onClick: async () => {
      pop.remove();
      const next = prompt("New title", conv.title || "");
      if (next !== null && next.trim()) {
        await apiJson(`/app/api/conversations/${conv.id}`, {
          method: "PATCH", body: { title: next.trim() },
        });
        conv.title = next.trim();
        if (state.active?.id === conv.id) el.convTitle.textContent = conv.title;
        renderConvList();
      }
    },
  }, [icon("edit", 13), h("span", { text: "Rename" })]);

  const remove = h("button", {
    class: "danger",
    onClick: async () => {
      pop.remove();
      if (!confirm(`Delete "${conv.title || "this conversation"}"?`)) return;
      await apiJson(`/app/api/conversations/${conv.id}`, { method: "DELETE" });
      state.conversations = state.conversations.filter((x) => x.id !== conv.id);
      if (state.active?.id === conv.id) {
        state.active = null;
        state.messages = [];
        el.convTitle.textContent = "New chat";
        renderMessages();
      }
      renderConvList();
    },
  }, [icon("trash", 13), h("span", { text: "Delete" })]);

  pop.append(rename, remove);
  document.body.append(pop);
  setTimeout(() => {
    const off = (e) => {
      if (!pop.contains(e.target)) {
        pop.remove();
        document.removeEventListener("click", off);
      }
    };
    document.addEventListener("click", off);
  }, 0);
}

// ---------------- Model picker ----------------

function modelMeta(id) {
  return MODEL_CATALOG[id] || { shortName: id, name: id, group: "Other", preview: false };
}

function updateModelChip() {
  const m = modelMeta(state.model);
  el.modelChipName.textContent = m.shortName;
}

function openModelPopover() {
  const existing = el.modelPicker.querySelector(".model-popover");
  if (existing) { existing.remove(); el.modelChip.setAttribute("aria-expanded", "false"); return; }

  const pop = h("div", { class: "model-popover", role: "listbox" });
  const groups = {};
  for (const id of VALID_MODELS) {
    const m = modelMeta(id);
    groups[m.group] = groups[m.group] || [];
    groups[m.group].push({ id, ...m });
  }
  for (const [groupName, models] of Object.entries(groups)) {
    pop.append(h("div", { class: "model-group-label", text: groupName }));
    for (const m of models) {
      const row = h("button", {
        type: "button",
        class: "model-option" + (m.id === state.model ? " active" : ""),
        onClick: () => {
          state.model = m.id;
          updateModelChip();
          persistSettings();
          pop.remove();
          el.modelChip.setAttribute("aria-expanded", "false");
        },
      }, [
        h("span", { class: "model-title" }, [
          h("span", { text: m.name }),
          m.preview ? h("span", { class: "badge badge-preview", text: "Preview" }) : null,
        ]),
        icon("check", 14, "model-check"),
      ]);
      pop.append(row);
    }
  }

  // Thinking effort row
  const effortRow = h("div", { class: "effort-row" }, [
    h("span", { class: "effort-label", text: "Thinking" }),
  ]);
  const effortOpts = h("div", { class: "effort-options" });
  for (const e of VALID_EFFORTS) {
    effortOpts.append(h("button", {
      type: "button",
      class: state.effort === e ? "active" : "",
      text: e,
      onClick: () => {
        state.effort = e;
        effortOpts.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.textContent === state.effort));
        persistSettings();
      },
    }));
  }
  effortRow.append(effortOpts);
  pop.append(effortRow);

  el.modelPicker.append(pop);
  el.modelChip.setAttribute("aria-expanded", "true");

  setTimeout(() => {
    const off = (e) => {
      if (!el.modelPicker.contains(e.target)) {
        pop.remove();
        el.modelChip.setAttribute("aria-expanded", "false");
        document.removeEventListener("mousedown", off);
      }
    };
    document.addEventListener("mousedown", off);
  }, 0);
}

el.modelChip.addEventListener("click", openModelPopover);

async function persistSettings() {
  if (!state.active) return;
  try {
    await apiJson(`/app/api/conversations/${state.active.id}`, {
      method: "PATCH",
      body: { model: state.model, effort: state.effort },
    });
    state.active.model = state.model;
    state.active.thinking_effort = state.effort;
  } catch (err) {
    console.warn("persistSettings failed", err);
  }
}

// ---------------- Messages rendering ----------------

function toolCategory(name) {
  if (!name) return null;
  if (name.startsWith("proxmox_")) return "proxmox";
  if (name.startsWith("bmc_")) return "bmc";
  if (name.startsWith("ssh_")) return "ssh";
  return null;
}
function toolIconName(name) {
  const cat = toolCategory(name);
  if (cat === "proxmox") return "server";
  if (cat === "bmc") return "cpu";
  if (cat === "ssh") return "terminal";
  return "bolt";
}
function toolNeedsConfirm(name, args) {
  // Unified run tools (ssh_run / proxmox_run) plus legacy *_exec_command*
  // names kept for backwards compatibility with older servers. Pure poll
  // calls (exec_id set, no command) are read-only and must not require a
  // confirmation click.
  const match = /^(ssh_run|proxmox_run|ssh_exec_command|proxmox_exec_command)(_|$)/.test(name || "");
  if (!match) return false;
  if ((name === "ssh_run" || name === "proxmox_run") && args && typeof args === "object") {
    if (args.exec_id && !args.command) return false;
  }
  return true;
}

function renderMessages() {
  el.messages.innerHTML = "";
  if (!state.messages.length) {
    el.messages.append(renderEmptyState());
    return;
  }
  const inner = h("div", { class: "messages-inner" });
  for (const m of state.messages) inner.append(renderMessage(m));
  el.messages.append(inner);
  scrollToBottom();
}

function renderEmptyState() {
  return h("div", { class: "empty" }, [
    h("h2", { text: "How can I help you manage your cluster?" }),
    h("p", { text: "Ask about nodes, VMs, BMC health, or run a command over SSH — with approval." }),
  ]);
}

function renderMessage(m) {
  const row = h("article", { class: `msg msg-${m.role}`, dataset: { id: m.id } });
  if (m.role === "user") {
    row.append(h("div", { class: "msg-body", text: m.content || "" }));
    return row;
  }

  // Assistant
  const modelLabel = modelMeta(m.model || state.model).name;
  row.append(h("div", { class: "msg-head" }, [h("span", { text: modelLabel })]));

  if (m.thinking_summary) {
    row.append(renderThinking(m.thinking_summary, false));
  }

  const toolCardMap = new Map();
  const body = h("div", { class: "msg-body" });
  body._raw = m.content || "";
  body.innerHTML = renderMarkdown(body._raw);
  row.append(body);
  for (const tc of m.tool_calls || []) {
    const card = renderToolCard(tc, m.id);
    toolCardMap.set(tc.id, card);
    row.append(card);
  }

  if (!m.streaming) {
    row.append(h("div", { class: "msg-actions" }, [
      h("button", {
        class: "msg-action", "aria-label": "Copy",
        onClick: () => {
          navigator.clipboard?.writeText(m.content || "");
        },
      }, [icon("copy", 14)]),
      h("button", { class: "msg-action", "aria-label": "Regenerate" }, [icon("regen", 14)]),
    ]));
  }

  // Stash ref so streaming can mutate body + tool cards in place.
  row._body = body;
  row._toolCardMap = toolCardMap;
  return row;
}

function insertStreamNode(row, node) {
  const tail = row.querySelector(".typing") || row.querySelector(".msg-actions");
  if (tail) row.insertBefore(node, tail);
  else row.append(node);
}

function ensureStreamingBody(layout) {
  let body = layout.bodyRef.current;
  if (body) return body;
  body = h("div", { class: "msg-body" });
  body._raw = "";
  insertStreamNode(layout.row, body);
  layout.bodyRef.current = body;
  return body;
}

function closeStreamingBody(layout) {
  const body = layout.bodyRef.current;
  if (!body) return;
  if (!(body._raw || "").trim()) body.remove();
  layout.bodyRef.current = null;
}

function renderThinking(text, active) {
  const block = h("div", { class: `thinking${active ? " active" : ""}` });
  const labelSpan = h("span", { class: "thinking-label", text: active ? "Thinking…" : "Thought for a moment" });
  const bodyInner = h("div", { class: "thinking-body-inner", text });
  const head = h("button", {
    class: "thinking-head", type: "button",
    onClick: () => block.classList.toggle("open"),
  }, [icon("sparkles", 13), labelSpan, icon("chev-d", 13, "chev")]);
  const body = h("div", { class: "thinking-body" }, [bodyInner]);
  block.append(head, body);
  block._labelSpan = labelSpan;
  block._bodyInner = bodyInner;
  return block;
}

function renderToolCard(tc, msgId) {
  const cat = toolCategory(tc.name);
  const card = h("div", {
    class: "tool-card" + (tc.status === "awaiting_confirm" ? " open" : ""),
    dataset: { id: tc.id, state: tc.status || "pending", cat: cat || "" },
  });
  const statusText = {
    pending: "Running…",
    ok: tc.duration_ms != null ? `${tc.duration_ms} ms` : "Done",
    success: tc.duration_ms != null ? `${tc.duration_ms} ms` : "Done",
    error: "Failed",
    awaiting_confirm: "Approval required",
    rejected: "Rejected",
  }[tc.status] || "";

  // Special prominent confirm card for shell commands awaiting approval.
  if (tc.status === "awaiting_confirm" && toolNeedsConfirm(tc.name, tc.args)) {
    return renderConfirmCard(tc, msgId);
  }

  const head = h("button", {
    class: "tool-head", type: "button",
    onClick: () => card.classList.toggle("open"),
  }, [
    h("div", { class: "tool-icon" }, [icon(toolIconName(tc.name), 14)]),
    h("span", { class: "tool-name", text: tc.name }),
    h("span", { class: "tool-status", text: statusText }),
    icon("chev-d", 13, "tool-chev"),
  ]);

  const inner = h("div", { class: "tool-body-inner" });
  if (tc.args && Object.keys(tc.args).length) {
    inner.append(h("div", { class: "tool-section-label", text: "Arguments" }));
    inner.append(h("pre", { class: "tool-block", text: JSON.stringify(tc.args, null, 2) }));
  }
  if (tc.preview) {
    inner.append(h("div", { class: "tool-section-label", text: "Result" }));
    inner.append(h("pre", { class: "tool-block", text: tc.preview }));
  }
  if (tc.status === "awaiting_confirm") {
    const approve = h("button", {
      type: "button", class: "btn btn-approve",
      onClick: () => sendConfirmation(tc.id, true, approve, reject),
    }, [icon("check", 13), h("span", { text: "Approve" })]);
    const reject = h("button", {
      type: "button", class: "btn btn-reject",
      onClick: () => sendConfirmation(tc.id, false, approve, reject),
    }, [icon("x", 13), h("span", { text: "Reject" })]);
    inner.append(h("div", { class: "confirm-actions", style: { marginTop: "10px" } }, [approve, reject]));
  }
  card.append(head, h("div", { class: "tool-body" }, [inner]));
  return card;
}

function renderConfirmCard(tc, msgId) {
  const host = (tc.args && (tc.args.host || tc.args.hostname)) || "";
  const cmd = (tc.args && (tc.args.command || tc.args.cmd || "")) || "";

  const actions = h("div", { class: "confirm-actions" });
  const approve = h("button", {
    type: "button", class: "btn btn-approve",
    onClick: () => sendConfirmation(tc.id, true, approve, reject),
  }, [icon("check", 13), h("span", { text: "Approve and run" })]);
  const reject = h("button", {
    type: "button", class: "btn btn-reject",
    onClick: () => sendConfirmation(tc.id, false, approve, reject),
  }, [icon("x", 13), h("span", { text: "Reject" })]);
  actions.append(approve, reject);

  const card = h("div", {
    class: "confirm-card", dataset: { id: tc.id, state: "awaiting_confirm" },
  }, [
    h("div", { class: "confirm-head" }, [
      h("div", { class: "confirm-icon" }, [icon("shield", 16)]),
      h("div", { style: { flex: "1" } }, [
        h("div", { class: "confirm-title", text: "Approval required to run command" }),
        h("div", { class: "confirm-sub" }, [
          h("code", { text: tc.name }),
          host ? document.createTextNode(" on ") : null,
          host ? h("span", { class: "host", text: host }) : null,
        ]),
      ]),
    ]),
    cmd ? h("pre", { class: "confirm-code", text: "$ " + cmd }) : null,
    actions,
  ]);
  return card;
}

async function sendConfirmation(callId, approve, approveBtn, rejectBtn) {
  approveBtn.disabled = true;
  rejectBtn.disabled = true;
  try {
    await apiJson("/app/api/chat/confirm", {
      method: "POST", body: { call_id: callId, approve },
    });
  } catch (err) {
    console.error(err);
    approveBtn.disabled = false;
    rejectBtn.disabled = false;
  }
}

function scrollToBottom() {
  el.messages.scrollTop = el.messages.scrollHeight;
}

// ---------------- Usage footer + modal ----------------

const stateUsage = { last: null, lastLoadedAt: 0 };

function pctOf(spent, limit) {
  if (!limit || limit <= 0) return 0;
  return Math.max(0, (spent / limit) * 100);
}
function formatPct(spent, limit) {
  if (!limit || limit <= 0) return "—";
  const p = pctOf(spent, limit);
  if (p < 10) return `${p.toFixed(1)}%`;
  return `${Math.round(p)}%`;
}
function formatResetIn(resetEpoch) {
  if (!resetEpoch) return "No active session";
  const now = Date.now() / 1000;
  const remaining = Math.max(0, resetEpoch - now);
  if (remaining <= 0) return "Resets on next request";
  const hours = Math.floor(remaining / 3600);
  const mins = Math.floor((remaining % 3600) / 60);
  if (hours === 0) return `Resets in ${mins} min`;
  return `Resets in ${hours} h ${String(mins).padStart(2, "0")}`;
}
function formatAgo(loadedAtMs) {
  if (!loadedAtMs) return "—";
  const diff = Math.max(0, Date.now() - loadedAtMs);
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return "Updated just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `Updated ${mins} min ago`;
  const hours = Math.floor(mins / 60);
  return `Updated ${hours} h ago`;
}

function renderUsage(u) {
  if (!u) return;
  stateUsage.last = u;
  stateUsage.lastLoadedAt = Date.now();

  const hasAnyCap = u.limit_5h_usd > 0 || u.limit_week_usd > 0;
  if (!hasAnyCap) {
    el.composerHint.hidden = true;
    return;
  }
  el.composerHint.hidden = false;

  if (u.limit_5h_usd > 0) {
    el.usage5hValue.textContent = formatPct(u.spent_5h_usd, u.limit_5h_usd);
    el.usage5h.hidden = false;
    el.usage5h.classList.toggle("usage-over", u.spent_5h_usd >= u.limit_5h_usd);
  } else {
    el.usage5h.hidden = true;
  }
  if (u.limit_week_usd > 0) {
    el.usageWeekValue.textContent = formatPct(u.spent_week_usd, u.limit_week_usd);
    el.usageWeek.hidden = false;
    el.usageWeek.classList.toggle("usage-over", u.spent_week_usd >= u.limit_week_usd);
  } else {
    el.usageWeek.hidden = true;
  }

  renderUsageModal(u);
}

function renderUsageModal(u) {
  if (!u) return;
  const p5 = pctOf(u.spent_5h_usd, u.limit_5h_usd);
  el.usageModal5hPct.textContent = formatPct(u.spent_5h_usd, u.limit_5h_usd);
  el.usageModal5hFill.style.width = `${Math.min(100, p5)}%`;
  el.usageModal5hReset.textContent = formatResetIn(u.session_5h_reset_at);
  el.usageSection5h.classList.toggle(
    "usage-over", u.limit_5h_usd > 0 && u.spent_5h_usd >= u.limit_5h_usd,
  );
  el.usageSection5h.hidden = !(u.limit_5h_usd > 0);

  const pw = pctOf(u.spent_week_usd, u.limit_week_usd);
  el.usageModalWeekPct.textContent = formatPct(u.spent_week_usd, u.limit_week_usd);
  el.usageModalWeekFill.style.width = `${Math.min(100, pw)}%`;
  el.usageSectionWeek.classList.toggle(
    "usage-over", u.limit_week_usd > 0 && u.spent_week_usd >= u.limit_week_usd,
  );
  el.usageSectionWeek.hidden = !(u.limit_week_usd > 0);

  el.usageModalUpdated.textContent = formatAgo(stateUsage.lastLoadedAt);
}

function openUsageModal() {
  if (!stateUsage.last) return;
  renderUsageModal(stateUsage.last);
  el.usageBackdrop.hidden = false;
  el.usageModal.hidden = false;
  el.usageModal.focus();
}
function closeUsageModal() {
  el.usageBackdrop.hidden = true;
  el.usageModal.hidden = true;
}

el.usage5h?.addEventListener("click", openUsageModal);
el.usageWeek?.addEventListener("click", openUsageModal);
el.usageModalClose?.addEventListener("click", closeUsageModal);
el.usageBackdrop?.addEventListener("click", closeUsageModal);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !el.usageModal.hidden) closeUsageModal();
});
el.usageModalRefresh?.addEventListener("click", async () => {
  el.usageModalRefresh.querySelector("svg")?.classList.add("spinning");
  try { await loadUsage(); }
  finally { el.usageModalRefresh.querySelector("svg")?.classList.remove("spinning"); }
});

async function loadUsage() {
  try {
    const data = await apiJson("/app/api/usage");
    renderUsage(data?.usage);
  } catch (err) {
    console.warn("usage load failed", err);
  }
}

// ---------------- Conversation API ----------------

async function loadConversations() {
  const data = await apiJson("/app/api/conversations");
  state.conversations = data.conversations || [];
  renderConvList();
}

async function loadConversation(id) {
  const data = await apiJson(`/app/api/conversations/${id}`);
  state.active = data.conversation;
  state.messages = data.messages || [];
  el.convTitle.textContent = state.active.title || "New chat";
  state.model = state.active.model || DEFAULT_MODEL;
  state.effort = state.active.thinking_effort || DEFAULT_EFFORT;
  updateModelChip();
  renderConvList();
  renderMessages();
}

async function createConversation() {
  const data = await apiJson("/app/api/conversations", {
    method: "POST",
    body: { model: state.model || DEFAULT_MODEL, effort: state.effort || DEFAULT_EFFORT },
  });
  const conv = data.conversation;
  state.conversations.unshift(conv);
  state.active = conv;
  state.messages = [];
  el.convTitle.textContent = "New chat";
  renderConvList();
  renderMessages();
  el.composer.focus();
}

// ---------------- Composer ----------------

function autogrow() {
  el.composer.style.height = "auto";
  const max = 240;
  el.composer.style.height = Math.min(el.composer.scrollHeight, max) + "px";
  el.send.disabled = !el.composer.value.trim() && !state.streaming;
}

el.composer.addEventListener("input", autogrow);
el.composer.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
});
el.send.addEventListener("click", submit);
el.newChat.addEventListener("click", async () => {
  await createConversation();
  closeSidebar();
});

async function submit() {
  if (state.streaming) {
    if (state.abortController) state.abortController.abort();
    return;
  }
  const text = el.composer.value.trim();
  if (!text) return;

  if (!state.active) await createConversation();

  // Empty-state becomes inner list.
  if (el.messages.querySelector(".empty")) {
    el.messages.innerHTML = "";
    el.messages.append(h("div", { class: "messages-inner" }));
  }
  let inner = el.messages.querySelector(".messages-inner");
  if (!inner) {
    inner = h("div", { class: "messages-inner" });
    el.messages.append(inner);
  }

  const optimistic = { id: `tmp-${Date.now()}`, role: "user", content: text, tool_calls: [] };
  state.messages.push(optimistic);
  inner.append(renderMessage(optimistic));
  scrollToBottom();

  el.composer.value = "";
  autogrow();

  await streamTurn(text, inner);
}

// ---------------- Streaming ----------------

async function streamTurn(userText, inner) {
  state.streaming = true;
  el.send.disabled = false;
  el.send.classList.add("stop");
  el.send.querySelector("svg use")?.setAttribute("href", "#i-stop");
  el.send.setAttribute("aria-label", "Stop");

  const controller = new AbortController();
  state.abortController = controller;

  const assistantMsg = {
    id: `pending-${Date.now()}`,
    role: "assistant",
    content: "",
    tool_calls: [],
    model: state.model,
    effort: state.effort,
    streaming: true,
  };
  state.messages.push(assistantMsg);
  const row = renderMessage(assistantMsg);
  inner.append(row);

  // Live thinking block is injected on the first thinking_delta.
  let thinkingBlock = null;
  const toolCardMap = row._toolCardMap;
  const layout = { row, bodyRef: { current: row._body } };

  const indicator = h("div", { class: "typing" }, [
    h("div", { class: "typing-dots" }, [h("span"), h("span"), h("span")]),
    h("span", { class: "typing-label", text: "Thinking…" }),
  ]);
  row.append(indicator);
  scrollToBottom();

  try {
    const res = await fetch("/app/api/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
        "Accept": "text/event-stream",
      },
      body: JSON.stringify({
        conversation_id: state.active.id,
        content: userText,
        model: state.model,
        effort: state.effort,
      }),
      credentials: "same-origin",
      signal: controller.signal,
    });

    if (res.status === 401) {
      window.location.href = "/app/refresh?next=/app/chat";
      return;
    }
    if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sepIdx;
      while ((sepIdx = buf.indexOf("\n\n")) !== -1) {
        const chunk = buf.slice(0, sepIdx);
        buf = buf.slice(sepIdx + 2);
        const ev = parseSseFrame(chunk);
        if (ev) {
          handleEvent(
            ev,
            assistantMsg,
            row,
            toolCardMap,
            { getThinking: () => thinkingBlock, setThinking: (b) => { thinkingBlock = b; } },
            layout,
          );
        }
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      console.error(err);
      row.append(h("div", { class: "banner banner-error", text: "Connection interrupted. Try again." }));
    }
  } finally {
    indicator.remove();
    if (thinkingBlock) {
      thinkingBlock.classList.remove("active");
      if (thinkingBlock._labelSpan) thinkingBlock._labelSpan.textContent = "Thought for a moment";
    }
    assistantMsg.streaming = false;
    state.streaming = false;
    state.abortController = null;
    el.send.classList.remove("stop");
    el.send.querySelector("svg use")?.setAttribute("href", "#i-send");
    el.send.setAttribute("aria-label", "Send");
    autogrow();

    // Append action buttons now that streaming is done.
    if (!row.querySelector(".msg-actions")) {
      row.append(h("div", { class: "msg-actions" }, [
        h("button", {
          class: "msg-action", "aria-label": "Copy",
          onClick: () => navigator.clipboard?.writeText(assistantMsg.content || ""),
        }, [icon("copy", 14)]),
        h("button", { class: "msg-action", "aria-label": "Regenerate" }, [icon("regen", 14)]),
      ]));
    }
  }
}

function parseSseFrame(chunk) {
  let event = null;
  const dataLines = [];
  for (const line of chunk.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!event) return null;
  try {
    const data = dataLines.length ? JSON.parse(dataLines.join("\n")) : {};
    return { event, data };
  } catch {
    return null;
  }
}

function handleEvent({ event, data }, assistantMsg, row, toolCardMap, thinkingCtx, layout) {
  switch (event) {
    case "text_delta": {
      const body = ensureStreamingBody(layout);
      body._raw = (body._raw || "") + (data.text || "");
      assistantMsg.content += data.text || "";
      body.innerHTML = renderMarkdown(body._raw);
      scrollToBottom();
      break;
    }
    case "thinking_delta": {
      assistantMsg.thinking_summary = (assistantMsg.thinking_summary || "") + data.summary;
      let tb = thinkingCtx.getThinking();
      if (!tb) {
        tb = renderThinking(assistantMsg.thinking_summary, true);
        // Thinking block comes first, before any tool card or body.
        row.insertBefore(tb, row.querySelector(".msg-head").nextSibling);
        thinkingCtx.setThinking(tb);
      } else {
        tb._bodyInner.textContent = assistantMsg.thinking_summary;
      }
      scrollToBottom();
      break;
    }
    case "tool_call": {
      closeStreamingBody(layout);
      const tc = {
        id: data.id, name: data.name, args: data.args || {},
        status: "pending", preview: null, duration_ms: null,
      };
      assistantMsg.tool_calls.push(tc);
      const card = renderToolCard(tc, assistantMsg.id);
      toolCardMap.set(data.id, { tc, card });
      insertStreamNode(layout.row, card);
      scrollToBottom();
      break;
    }
    case "tool_confirm_required": {
      let entry = toolCardMap.get(data.id);
      if (!entry) {
        closeStreamingBody(layout);
        const tc = {
          id: data.id, name: data.name, args: data.args || {},
          status: "awaiting_confirm", preview: null, duration_ms: null,
        };
        assistantMsg.tool_calls.push(tc);
        const card = renderToolCard(tc, assistantMsg.id);
        toolCardMap.set(data.id, { tc, card });
        insertStreamNode(layout.row, card);
      } else {
        entry.tc.status = "awaiting_confirm";
        const fresh = renderToolCard(entry.tc, assistantMsg.id);
        entry.card.replaceWith(fresh);
        entry.card = fresh;
      }
      scrollToBottom();
      break;
    }
    case "tool_result": {
      const entry = toolCardMap.get(data.id);
      if (!entry) break;
      entry.tc.status = data.status;
      entry.tc.preview = data.preview;
      entry.tc.duration_ms = data.duration_ms;
      const fresh = renderToolCard(entry.tc, assistantMsg.id);
      entry.card.replaceWith(fresh);
      entry.card = fresh;
      break;
    }
    case "session_expired": {
      window.location.href = "/app/refresh?next=/app/chat";
      break;
    }
    case "usage_update": {
      renderUsage(data);
      break;
    }
    case "title_updated": {
      if (state.active && state.active.id === data.conversation_id) {
        state.active.title = data.title;
        el.convTitle.textContent = data.title;
        const conv = state.conversations.find((c) => c.id === data.conversation_id);
        if (conv) { conv.title = data.title; renderConvList(); }
      }
      break;
    }
    case "error": {
      row.append(h("div", { class: "banner banner-error", text: `Error: ${data.message || data.code}` }));
      break;
    }
    case "done": {
      assistantMsg.id = data.message_id;
      row.dataset.id = data.message_id;
      break;
    }
    case "aborted": break;
  }
}

// ---------------- Boot ----------------

(async () => {
  state.model = DEFAULT_MODEL;
  state.effort = DEFAULT_EFFORT;
  updateModelChip();
  try {
    await Promise.all([loadConversations(), loadUsage()]);
    if (state.conversations.length) {
      await loadConversation(state.conversations[0].id);
    } else {
      renderMessages();
    }
  } catch (err) {
    console.error(err);
  }
})();

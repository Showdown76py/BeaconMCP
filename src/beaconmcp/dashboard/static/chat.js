// BeaconMCP dashboard chat client.
// Vanilla ES module. No emoji.

const root = document.querySelector(".chat-root");
if (!root) throw new Error("chat-root missing");

const CLIENT_ID = root.dataset.clientId;
const CLIENT_NAME = root.dataset.clientName;
const DEFAULT_MODEL = root.dataset.defaultModel;
const DEFAULT_EFFORT = root.dataset.defaultEffort;
const VALID_MODELS = JSON.parse(root.dataset.validModels || "[]");
const VALID_EFFORTS = JSON.parse(root.dataset.validEfforts || "[]");

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

// ---------------- Minimal safe markdown ----------------

const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}

// Extract fenced code blocks first so their body is never touched by the
// other markdown rules (headings inside a code block stay as text, etc).
// Returns { stripped, blocks } where placeholders ⟦CODE0⟧ are restored
// after all other passes have run.
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

// Inline pass: runs on a single line's text AFTER HTML-escaping. Inline
// code first so nothing inside backticks gets formatted.
function _renderInline(text) {
  // Inline code
  let out = text.replace(/`([^`\n]+?)`/g, (_, c) => `<code>${c}</code>`);
  // Bold then italic then strike
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)/g, "<em>$1</em>");
  out = out.replace(/~~([^~\n]+?)~~/g, "<s>$1</s>");
  // Links [label](https://...)
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, label, href) =>
      `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`,
  );
  return out;
}

function _isBulletLine(line) {
  return /^\s*[-*+]\s+/.test(line);
}
function _isOrderedLine(line) {
  return /^\s*\d+\.\s+/.test(line);
}
function _stripBullet(line) {
  return line.replace(/^\s*[-*+]\s+/, "");
}
function _stripOrdered(line) {
  return line.replace(/^\s*\d+\.\s+/, "");
}

// GitHub-flavored table row: splits `| a | b |` into ["a", "b"]. Handles
// optional leading/trailing pipes. Escaped pipes (\|) are preserved as "|".
function _splitTableRow(line) {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells = [];
  let buf = "";
  for (let k = 0; k < trimmed.length; k += 1) {
    const ch = trimmed[k];
    if (ch === "\\" && trimmed[k + 1] === "|") {
      buf += "|";
      k += 1;
      continue;
    }
    if (ch === "|") {
      cells.push(buf.trim());
      buf = "";
      continue;
    }
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
  // 1. Pull fenced code blocks out so nothing munges their contents.
  const { stripped, blocks } = _extractFences(text);

  // 2. Work line-by-line so we can recognise block-level constructs
  // (headings, lists, blockquotes, hr). Inline formatting is applied
  // after HTML-escaping each line body.
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

    // Blank line -> paragraph break
    if (/^\s*$/.test(line)) {
      flushParagraph();
      i += 1;
      continue;
    }

    // Horizontal rule: ---, ***, ___ on their own line
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushParagraph();
      out.push("<hr>");
      i += 1;
      continue;
    }

    // ATX heading: 1-6 # followed by space
    const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      const body = _renderInline(escapeHtml(heading[2]));
      out.push(`<h${level}>${body}</h${level}>`);
      i += 1;
      continue;
    }

    // Blockquote: consume consecutive "> " lines
    if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      const quoted = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      const body = quoted.map((l) => _renderInline(escapeHtml(l))).join("<br>");
      out.push(`<blockquote>${body}</blockquote>`);
      continue;
    }

    // GFM table: header row followed by a separator row (| --- | --- |)
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
      const alignAttr = (idx) =>
        aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
      const thead = `<thead><tr>${headers
        .map((h, idx) => `<th${alignAttr(idx)}>${_renderInline(escapeHtml(h))}</th>`)
        .join("")}</tr></thead>`;
      const tbody = `<tbody>${rows
        .map(
          (r) =>
            `<tr>${r
              .map(
                (c, idx) =>
                  `<td${alignAttr(idx)}>${_renderInline(escapeHtml(c))}</td>`,
              )
              .join("")}</tr>`,
        )
        .join("")}</tbody>`;
      out.push(`<table class="md-table">${thead}${tbody}</table>`);
      continue;
    }

    // Unordered list: consume consecutive bullet lines
    if (_isBulletLine(line)) {
      flushParagraph();
      const items = [];
      while (i < lines.length && _isBulletLine(lines[i])) {
        items.push(_stripBullet(lines[i]));
        i += 1;
      }
      out.push(
        `<ul>${items
          .map((it) => `<li>${_renderInline(escapeHtml(it))}</li>`)
          .join("")}</ul>`,
      );
      continue;
    }

    // Ordered list
    if (_isOrderedLine(line)) {
      flushParagraph();
      const items = [];
      while (i < lines.length && _isOrderedLine(lines[i])) {
        items.push(_stripOrdered(lines[i]));
        i += 1;
      }
      out.push(
        `<ol>${items
          .map((it) => `<li>${_renderInline(escapeHtml(it))}</li>`)
          .join("")}</ol>`,
      );
      continue;
    }

    // Default: accumulate into current paragraph.
    inParagraph.push(line);
    i += 1;
  }
  flushParagraph();

  let html = out.join("");

  // 3. Restore fenced code blocks.
  html = html.replace(/\u0000CODEBLOCK(\d+)\u0000/g, (_, idx) => {
    const { lang, body } = blocks[Number(idx)];
    const cls = lang ? ` class="lang-${escapeHtml(lang)}"` : "";
    return `<pre><code${cls}>${escapeHtml(body)}</code></pre>`;
  });

  // A paragraph that wraps a code-block placeholder-turned-<pre> breaks
  // layout; unwrap those (the extraction happens before paragraph
  // splitting so this is defensive against any edge case).
  html = html.replace(/<p>(<pre>[\s\S]*?<\/pre>)<\/p>/g, "$1");
  return html;
}

// ---------------- API helpers ----------------

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
  active: null, // Conversation
  messages: [], // Message[]
  streaming: false,
  abortController: null,
};

// ---------------- DOM refs ----------------

const el = {
  sidebar: document.getElementById("sidebar"),
  openSidebar: document.getElementById("open-sidebar"),
  closeSidebar: document.getElementById("close-sidebar"),
  backdrop: document.getElementById("sidebar-backdrop"),
  newChat: document.getElementById("new-chat"),
  convList: document.getElementById("conv-list"),
  convTitle: document.getElementById("conv-title"),
  convMenu: document.getElementById("conv-menu"),
  messages: document.getElementById("messages"),
  composer: document.getElementById("composer-input"),
  send: document.getElementById("composer-send"),
  selModel: document.getElementById("select-model"),
  selEffort: document.getElementById("select-effort"),
  usageBar: document.getElementById("usage-bar"),
  usage5hValue: document.getElementById("usage-5h-value"),
  usageWeekValue: document.getElementById("usage-week-value"),
  usageModal: document.getElementById("usage-modal"),
  usageModalBackdrop: document.getElementById("usage-modal-backdrop"),
  usageModalClose: document.querySelector(".usage-modal-close"),
  usageModal5hPct: document.getElementById("usage-modal-5h-pct"),
  usageModal5hReset: document.getElementById("usage-modal-5h-reset"),
  usageModal5hFill: document.getElementById("usage-modal-5h-fill"),
  usageModalWeekPct: document.getElementById("usage-modal-week-pct"),
  usageModalWeekFill: document.getElementById("usage-modal-week-fill"),
  usageModalUpdated: document.getElementById("usage-modal-updated"),
  usageModalRefresh: document.getElementById("usage-modal-refresh"),
};

// ---------------- Sidebar ----------------

function openSidebar() {
  el.sidebar.classList.add("open");
  el.backdrop.hidden = false;
}
function closeSidebar() {
  el.sidebar.classList.remove("open");
  el.backdrop.hidden = true;
}
el.openSidebar?.addEventListener("click", openSidebar);
el.closeSidebar?.addEventListener("click", closeSidebar);
el.backdrop?.addEventListener("click", closeSidebar);

// ---------------- Conversation list ----------------

function renderConvList() {
  el.convList.innerHTML = "";
  for (const c of state.conversations) {
    const item = document.createElement("a");
    item.className = "conv-item" + (state.active?.id === c.id ? " active" : "");
    item.href = "#";
    item.dataset.id = c.id;
    const title = document.createElement("span");
    title.className = "conv-title";
    title.textContent = c.title || "Nouveau chat";
    const menuBtn = document.createElement("button");
    menuBtn.className = "icon-btn conv-menu-btn";
    menuBtn.setAttribute("aria-label", "Options");
    menuBtn.innerHTML = '<svg width="16" height="16"><use href="#i-more"/></svg>';
    menuBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openConvMenu(c, menuBtn);
    });
    item.append(title, menuBtn);
    item.addEventListener("click", (e) => {
      e.preventDefault();
      loadConversation(c.id);
      closeSidebar();
    });
    el.convList.append(item);
  }
}

function openConvMenu(conv, anchor) {
  const existing = document.querySelector(".conv-popover");
  if (existing) existing.remove();
  const pop = document.createElement("div");
  pop.className = "conv-popover";
  const rect = anchor.getBoundingClientRect();
  pop.style.top = `${rect.bottom + 4}px`;
  pop.style.left = `${rect.left - 140}px`;

  const rename = document.createElement("button");
  rename.innerHTML = '<svg width="14" height="14"><use href="#i-edit"/></svg><span>Renommer</span>';
  rename.addEventListener("click", async () => {
    pop.remove();
    const next = prompt("Nouveau titre", conv.title || "");
    if (next !== null && next.trim()) {
      await apiJson(`/app/api/conversations/${conv.id}`, {
        method: "PATCH",
        body: { title: next.trim() },
      });
      conv.title = next.trim();
      if (state.active?.id === conv.id) el.convTitle.textContent = conv.title;
      renderConvList();
    }
  });

  const remove = document.createElement("button");
  remove.className = "danger";
  remove.innerHTML = '<svg width="14" height="14"><use href="#i-trash"/></svg><span>Supprimer</span>';
  remove.addEventListener("click", async () => {
    pop.remove();
    if (!confirm(`Supprimer "${conv.title || "cette conversation"}" ?`)) return;
    await apiJson(`/app/api/conversations/${conv.id}`, { method: "DELETE" });
    state.conversations = state.conversations.filter((x) => x.id !== conv.id);
    if (state.active?.id === conv.id) {
      state.active = null;
      state.messages = [];
      el.convTitle.textContent = "Nouveau chat";
      renderMessages();
    }
    renderConvList();
  });

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

// ---------------- Messages rendering ----------------

function renderMessages() {
  el.messages.innerHTML = "";
  for (const m of state.messages) {
    el.messages.append(renderMessage(m));
  }
  scrollToBottom();
}

function renderMessage(m) {
  const row = document.createElement("article");
  row.className = `msg msg-${m.role}`;
  row.dataset.id = m.id;

  if (m.role === "assistant") {
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.textContent = `${m.model || DEFAULT_MODEL} · ${m.effort || DEFAULT_EFFORT}`;
    row.append(meta);
  }

  // Tool cards come before the text; they represent what the assistant did
  // before producing its final answer.
  if (m.tool_calls && m.tool_calls.length) {
    for (const tc of m.tool_calls) {
      row.append(renderToolCard(tc));
    }
  }

  if (m.role === "user") {
    const body = document.createElement("div");
    body.className = "msg-body";
    body.textContent = m.content || "";
    row.append(body);
  } else {
    const body = document.createElement("div");
    body.className = "msg-body md";
    body.innerHTML = renderMarkdown(m.content || "");
    row.append(body);
  }

  return row;
}

function renderToolCard(tc) {
  const card = document.createElement("details");
  const stateClass = tc.status === "awaiting_confirm"
    ? "tool-awaiting"
    : `tool-${tc.status || "pending"}`;
  card.className = `tool-card ${stateClass}`;
  card.dataset.id = tc.id;
  // Auto-expand cards waiting for approval so the user sees the args.
  if (tc.status === "awaiting_confirm") card.open = true;

  const sum = document.createElement("summary");
  const icon = document.createElement("svg");
  icon.setAttribute("width", "14");
  icon.setAttribute("height", "14");
  icon.setAttribute("aria-hidden", "true");
  const iconId = tc.status === "ok" ? "#i-check"
    : tc.status === "error" ? "#i-warn"
    : tc.status === "rejected" ? "#i-x"
    : tc.status === "awaiting_confirm" ? "#i-warn"
    : "#i-spin";
  icon.innerHTML = `<use href="${iconId}"/>`;
  sum.append(icon);

  const name = document.createElement("span");
  name.className = "tool-name";
  name.textContent = tc.name;
  sum.append(name);

  if (tc.status === "awaiting_confirm") {
    const badge = document.createElement("span");
    badge.className = "tool-badge";
    badge.textContent = "approbation requise";
    sum.append(badge);
  }

  if (tc.duration_ms != null) {
    const dur = document.createElement("span");
    dur.className = "tool-dur";
    dur.textContent = `${tc.duration_ms} ms`;
    sum.append(dur);
  }
  card.append(sum);

  const detail = document.createElement("div");
  detail.className = "tool-detail";

  if (Object.keys(tc.args || {}).length) {
    const argsBlock = document.createElement("pre");
    argsBlock.className = "tool-block";
    argsBlock.textContent = `args: ${JSON.stringify(tc.args, null, 2)}`;
    detail.append(argsBlock);
  }
  if (tc.preview) {
    const resBlock = document.createElement("pre");
    resBlock.className = "tool-block";
    resBlock.textContent = `result: ${tc.preview}`;
    detail.append(resBlock);
  }

  if (tc.status === "awaiting_confirm") {
    const actions = document.createElement("div");
    actions.className = "tool-confirm-actions";

    const approve = document.createElement("button");
    approve.type = "button";
    approve.className = "btn btn-approve";
    approve.textContent = "Autoriser";
    approve.addEventListener("click", () => sendConfirmation(tc.id, true, approve, reject));

    const reject = document.createElement("button");
    reject.type = "button";
    reject.className = "btn btn-reject";
    reject.textContent = "Refuser";
    reject.addEventListener("click", () => sendConfirmation(tc.id, false, approve, reject));

    actions.append(approve, reject);
    detail.append(actions);
  }

  card.append(detail);
  return card;
}

async function sendConfirmation(callId, approve, approveBtn, rejectBtn) {
  approveBtn.disabled = true;
  rejectBtn.disabled = true;
  try {
    await apiJson("/app/api/chat/confirm", {
      method: "POST",
      body: { call_id: callId, approve },
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

const state_usage = { last: null, lastLoadedAt: 0 };

function pctOf(spent, limit) {
  if (!limit || limit <= 0) return 0;
  return Math.max(0, (spent / limit) * 100);
}

function formatPct(spent, limit) {
  if (!limit || limit <= 0) return "—";
  const p = pctOf(spent, limit);
  // Show 1 decimal under 10%, integer above for readability.
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
  if (secs < 60) return "less than a minute ago";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  return `${hours} h ago`;
}

function renderUsage(u) {
  if (!u) return;
  state_usage.last = u;
  state_usage.lastLoadedAt = Date.now();

  const hasAnyCap = (u.limit_5h_usd > 0) || (u.limit_week_usd > 0);
  if (!hasAnyCap) {
    el.usageBar.hidden = true;
    return;
  }
  el.usageBar.hidden = false;

  // Footer: compact percentages.
  if (u.limit_5h_usd > 0) {
    el.usage5hValue.textContent = formatPct(u.spent_5h_usd, u.limit_5h_usd);
    el.usage5hValue.parentElement.classList.toggle(
      "usage-over", u.spent_5h_usd >= u.limit_5h_usd,
    );
    el.usage5hValue.parentElement.hidden = false;
  } else {
    el.usage5hValue.parentElement.hidden = true;
  }
  if (u.limit_week_usd > 0) {
    el.usageWeekValue.textContent = formatPct(u.spent_week_usd, u.limit_week_usd);
    el.usageWeekValue.parentElement.classList.toggle(
      "usage-over", u.spent_week_usd >= u.limit_week_usd,
    );
    el.usageWeekValue.parentElement.hidden = false;
  } else {
    el.usageWeekValue.parentElement.hidden = true;
  }

  // Modal: progress bars + reset labels.
  renderUsageModal(u);
}

function renderUsageModal(u) {
  if (!u) return;
  const p5 = pctOf(u.spent_5h_usd, u.limit_5h_usd);
  el.usageModal5hPct.textContent = formatPct(u.spent_5h_usd, u.limit_5h_usd);
  el.usageModal5hFill.style.width = `${Math.min(100, p5)}%`;
  el.usageModal5hReset.textContent = formatResetIn(u.session_5h_reset_at);
  el.usageModal5hFill.parentElement.parentElement.classList.toggle(
    "usage-over", u.limit_5h_usd > 0 && u.spent_5h_usd >= u.limit_5h_usd,
  );

  const pw = pctOf(u.spent_week_usd, u.limit_week_usd);
  el.usageModalWeekPct.textContent = formatPct(u.spent_week_usd, u.limit_week_usd);
  el.usageModalWeekFill.style.width = `${Math.min(100, pw)}%`;
  el.usageModalWeekFill.parentElement.parentElement.classList.toggle(
    "usage-over", u.limit_week_usd > 0 && u.spent_week_usd >= u.limit_week_usd,
  );

  el.usageModalUpdated.textContent =
    `Last updated: ${formatAgo(state_usage.lastLoadedAt)}`;
}

function openUsageModal() {
  if (!state_usage.last) return;
  renderUsageModal(state_usage.last);
  el.usageModalBackdrop.hidden = false;
  el.usageModal.hidden = false;
  el.usageModal.focus();
}

function closeUsageModal() {
  el.usageModalBackdrop.hidden = true;
  el.usageModal.hidden = true;
}

async function loadUsage() {
  try {
    const data = await apiJson("/app/api/usage");
    renderUsage(data?.usage);
  } catch (err) {
    // Not fatal: usage tracking is best-effort.
    console.warn("usage load failed", err);
  }
}

el.usageBar?.addEventListener("click", openUsageModal);
el.usageModalClose?.addEventListener("click", closeUsageModal);
el.usageModalBackdrop?.addEventListener("click", closeUsageModal);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !el.usageModal.hidden) closeUsageModal();
});
el.usageModalRefresh?.addEventListener("click", async () => {
  el.usageModalRefresh.classList.add("spinning");
  try { await loadUsage(); }
  finally { el.usageModalRefresh.classList.remove("spinning"); }
});

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
  el.convTitle.textContent = state.active.title || "Nouveau chat";
  el.selModel.value = state.active.model;
  el.selEffort.value = state.active.thinking_effort;
  renderConvList();
  renderMessages();
}

async function createConversation() {
  const data = await apiJson("/app/api/conversations", {
    method: "POST",
    body: {
      model: el.selModel.value || DEFAULT_MODEL,
      effort: el.selEffort.value || DEFAULT_EFFORT,
    },
  });
  const conv = data.conversation;
  state.conversations.unshift(conv);
  state.active = conv;
  state.messages = [];
  el.convTitle.textContent = "Nouveau chat";
  renderConvList();
  renderMessages();
  el.composer.focus();
}

// ---------------- Composer ----------------

function autogrow() {
  el.composer.style.height = "auto";
  const max = 220; // ~10 lines
  el.composer.style.height = Math.min(el.composer.scrollHeight, max) + "px";
}

el.composer.addEventListener("input", autogrow);
el.composer.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    submit();
  }
});
el.send.addEventListener("click", submit);
el.newChat.addEventListener("click", createConversation);

el.selModel.addEventListener("change", () => persistSettings());
el.selEffort.addEventListener("change", () => persistSettings());

async function persistSettings() {
  if (!state.active) return;
  await apiJson(`/app/api/conversations/${state.active.id}`, {
    method: "PATCH",
    body: { model: el.selModel.value, effort: el.selEffort.value },
  });
  state.active.model = el.selModel.value;
  state.active.thinking_effort = el.selEffort.value;
}

async function submit() {
  if (state.streaming) {
    if (state.abortController) state.abortController.abort();
    return;
  }
  const text = el.composer.value.trim();
  if (!text) return;

  if (!state.active) {
    await createConversation();
  }

  // Optimistic user bubble
  const optimistic = {
    id: `tmp-${Date.now()}`,
    role: "user",
    content: text,
    tool_calls: [],
  };
  state.messages.push(optimistic);
  el.messages.append(renderMessage(optimistic));
  scrollToBottom();

  el.composer.value = "";
  autogrow();

  await streamTurn(text);
}

// ---------------- Streaming ----------------

async function streamTurn(userText) {
  state.streaming = true;
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
    model: el.selModel.value,
    effort: el.selEffort.value,
  };
  state.messages.push(assistantMsg);
  const row = renderMessage(assistantMsg);
  el.messages.append(row);

  const body = row.querySelector(".msg-body");
  const toolCardMap = new Map();

  // "Generating…" indicator, pinned to the bottom of the turn so the user
  // sees something alive during thinking and between tool calls. Removed
  // in the finally block below.
  const indicator = document.createElement("div");
  indicator.className = "generating-indicator";
  indicator.innerHTML =
    '<span class="generating-dot"></span><span class="generating-label">Thinking…</span>';
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
        model: el.selModel.value,
        effort: el.selEffort.value,
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
        if (ev) handleEvent(ev, assistantMsg, row, body, toolCardMap);
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      console.error(err);
      const errDiv = document.createElement("div");
      errDiv.className = "banner banner-error";
      errDiv.textContent = "Connection interrupted. Try again.";
      row.append(errDiv);
    }
  } finally {
    indicator.remove();
    state.streaming = false;
    state.abortController = null;
    el.send.classList.remove("stop");
    el.send.querySelector("svg use")?.setAttribute("href", "#i-send");
    el.send.setAttribute("aria-label", "Send");
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

function handleEvent({ event, data }, assistantMsg, row, body, toolCardMap) {
  switch (event) {
    case "text_delta": {
      assistantMsg.content += data.text;
      body.innerHTML = renderMarkdown(assistantMsg.content);
      scrollToBottom();
      break;
    }
    case "thinking_delta": {
      assistantMsg.thinking_summary = (assistantMsg.thinking_summary || "") + data.summary;
      break;
    }
    case "tool_call": {
      const tc = {
        id: data.id, name: data.name, args: data.args || {},
        status: "pending", preview: null, duration_ms: null,
      };
      assistantMsg.tool_calls.push(tc);
      const card = renderToolCard(tc);
      toolCardMap.set(data.id, { tc, card });
      body.before(card);
      scrollToBottom();
      break;
    }
    case "tool_confirm_required": {
      // Either the tool_call event already landed (upgrade existing
      // card to awaiting_confirm), or it didn't (build a fresh card
      // straight in that state).
      let entry = toolCardMap.get(data.id);
      if (!entry) {
        const tc = {
          id: data.id, name: data.name, args: data.args || {},
          status: "awaiting_confirm", preview: null, duration_ms: null,
        };
        assistantMsg.tool_calls.push(tc);
        const card = renderToolCard(tc);
        toolCardMap.set(data.id, { tc, card });
        body.before(card);
      } else {
        entry.tc.status = "awaiting_confirm";
        const fresh = renderToolCard(entry.tc);
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
      // Re-render the card in place
      const fresh = renderToolCard(entry.tc);
      entry.card.replaceWith(fresh);
      entry.card = fresh;
      toolCardMap.set(data.id, entry);
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
        if (conv) {
          conv.title = data.title;
          renderConvList();
        }
      }
      break;
    }
    case "error": {
      const errDiv = document.createElement("div");
      errDiv.className = "banner banner-error";
      errDiv.textContent = `Erreur: ${data.message || data.code}`;
      row.append(errDiv);
      break;
    }
    case "done": {
      assistantMsg.id = data.message_id;
      row.dataset.id = data.message_id;
      break;
    }
    case "aborted": {
      // ignore, the fetch loop will stop
      break;
    }
  }
}

// ---------------- Boot ----------------

(async () => {
  try {
    await Promise.all([loadConversations(), loadUsage()]);
    if (state.conversations.length) {
      await loadConversation(state.conversations[0].id);
    }
  } catch (err) {
    console.error(err);
  }
})();

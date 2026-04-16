// TarkaMCP dashboard chat client.
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
  getCookie("tarkamcp_csrf_token") || "";

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

function renderMarkdown(text) {
  // Order: code fences -> inline code -> bold/italic/strike -> links -> line breaks.
  let out = escapeHtml(text);

  // Fenced code blocks
  out = out.replace(/```([a-zA-Z0-9+_-]*)\n([\s\S]*?)```/g, (_, lang, body) =>
    `<pre><code${lang ? ` class="lang-${escapeHtml(lang)}"` : ""}>${body.replace(/\n+$/, "")}</code></pre>`
  );

  // Inline code
  out = out.replace(/`([^`\n]+?)`/g, (_, c) => `<code>${c}</code>`);

  // Bold / italic / strikethrough
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)/g, "<em>$1</em>");
  out = out.replace(/~~([^~\n]+?)~~/g, "<s>$1</s>");

  // Links [text](https://...)
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, label, href) =>
      `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`
  );

  // Two newlines = paragraph, single newline = <br>
  out = out
    .split(/\n{2,}/)
    .map((p) => p.replace(/\n/g, "<br>"))
    .map((p) => `<p>${p}</p>`)
    .join("");
  // Pre blocks should not be wrapped in <p>
  out = out.replace(/<p>(<pre>[\s\S]*?<\/pre>)<\/p>/g, "$1");
  return out;
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
  card.className = `tool-card tool-${tc.status || "pending"}`;
  card.dataset.id = tc.id;

  const sum = document.createElement("summary");
  const icon = document.createElement("svg");
  icon.setAttribute("width", "14");
  icon.setAttribute("height", "14");
  icon.setAttribute("aria-hidden", "true");
  const iconId = tc.status === "ok" ? "#i-check"
    : tc.status === "error" ? "#i-warn" : "#i-spin";
  icon.innerHTML = `<use href="${iconId}"/>`;
  sum.append(icon);

  const name = document.createElement("span");
  name.className = "tool-name";
  name.textContent = tc.name;
  sum.append(name);

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
  card.append(detail);
  return card;
}

function scrollToBottom() {
  el.messages.scrollTop = el.messages.scrollHeight;
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
  el.send.setAttribute("aria-label", "Arrêter");

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
      errDiv.textContent = "Connexion interrompue. Réessaie.";
      row.append(errDiv);
    }
  } finally {
    state.streaming = false;
    state.abortController = null;
    el.send.classList.remove("stop");
    el.send.querySelector("svg use")?.setAttribute("href", "#i-send");
    el.send.setAttribute("aria-label", "Envoyer");
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
    await loadConversations();
    if (state.conversations.length) {
      await loadConversation(state.conversations[0].id);
    }
  } catch (err) {
    console.error(err);
  }
})();

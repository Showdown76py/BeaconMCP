// Shared MCP Apps client bridge.
//
// JSON-RPC 2.0 over postMessage to the host, per the ext-apps spec. Injected
// into each ui:// document by panel.py in place of the <!--mcp-bridge-->
// marker, so the panels stay single self-contained resources.
//
// Deliberately small and dependency-free: an app resource is preloaded by the
// host before the tool even runs, so every kilobyte is paid up front.

const MCPApp = (() => {
  "use strict";

  const PROTOCOL_VERSION = "2026-01-26";

  let nextId = 0;
  const pending = new Map();
  let hostCapabilities = {};
  let onToolResult = null;
  let onHostContext = null;
  let ready = false;

  function post(message) {
    window.parent.postMessage(message, "*");
  }

  function request(method, params) {
    const id = ++nextId;
    post({ jsonrpc: "2.0", id, method, params });
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  }

  function notify(method, params) {
    post({ jsonrpc: "2.0", method, params });
  }

  // The host owns the iframe height, so it has to be told when the content
  // reflows -- otherwise the panel renders into a fixed sliver.
  function reportSize() {
    notify("ui/notifications/size-changed", {
      width: document.documentElement.scrollWidth,
      height: document.documentElement.scrollHeight,
    });
  }

  // A CallToolResult carries the dict in structuredContent, but a host that
  // strips it still sends the JSON as text -- fall back rather than blank out.
  function unwrap(result) {
    if (result && result.structuredContent) return result.structuredContent;
    const first = result && result.content && result.content[0];
    if (first && first.type === "text") {
      try { return JSON.parse(first.text); } catch { return null; }
    }
    return null;
  }

  async function callTool(name, args) {
    const result = await request("tools/call", { name, arguments: args });
    const data = unwrap(result);
    if (result && result.isError) {
      throw new Error((data && data.error) || "tool call failed");
    }
    if (data && data.error) throw new Error(data.error);
    return data;
  }

  // Both of these are gated on host capabilities. Calling one the host did not
  // advertise gets an error back, so check first and no-op quietly: a panel
  // that works everywhere beats one that throws on a stricter host.
  function updateModelContext(structuredContent, text) {
    if (!hostCapabilities.updateModelContext) return Promise.resolve(false);
    return request("ui/update-model-context", {
      content: text ? [{ type: "text", text }] : undefined,
      structuredContent,
    }).then(() => true, () => false);
  }

  function sendMessage(text) {
    if (!hostCapabilities.message) return Promise.resolve(false);
    return request("ui/message", {
      role: "user",
      content: [{ type: "text", text }],
    }).then((r) => !(r && r.isError), () => false);
  }

  function requestDisplayMode(mode) {
    return request("ui/request-display-mode", { mode }).then(
      (r) => (r && r.mode) || null,
      () => null,
    );
  }

  window.addEventListener("message", (event) => {
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;

    if (message.id != null && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message || "request failed"));
      else resolve(message.result);
      return;
    }

    switch (message.method) {
      case "ui/notifications/tool-result":
        if (onToolResult) onToolResult(unwrap(message.params));
        break;
      case "ui/notifications/host-context-changed":
        if (onHostContext) onHostContext(message.params);
        break;
      case "ui/resource-teardown":
        post({ jsonrpc: "2.0", id: message.id, result: {} });
        break;
    }
  });

  function applyHostContext(ctx) {
    if (!ctx) return;
    if (ctx.theme) document.documentElement.dataset.theme = ctx.theme;
    const vars = ctx.styles && ctx.styles.variables;
    if (vars) {
      for (const [key, value] of Object.entries(vars)) {
        document.documentElement.style.setProperty(key, value);
      }
    }
  }

  /**
   * Perform the ui/initialize handshake.
   *
   * Params are flat: appInfo / appCapabilities / protocolVersion. Nesting the
   * capabilities or sending clientInfo instead of appInfo fails the host's
   * schema check, and a rejected handshake is silent -- the host simply never
   * answers, so the app never sends `initialized` and the host never delivers
   * the tool result. See @modelcontextprotocol/ext-apps App.connect().
   *
   * `onFail` is called if the host never completes the handshake, so the panel
   * can say so instead of sitting on a spinner forever.
   */
  function connect({ name, version = "1.0.0", onResult, onContext, onFail }) {
    onToolResult = onResult;
    onHostContext = (ctx) => { applyHostContext(ctx); if (onContext) onContext(ctx); };

    setTimeout(() => {
      if (!ready && onFail) {
        onFail(new Error("The host did not complete the ui/initialize handshake."));
      }
    }, 5000);

    return request("ui/initialize", {
      appInfo: { name, version },
      appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
      protocolVersion: PROTOCOL_VERSION,
    }).then((result) => {
      ready = true;
      hostCapabilities = (result && result.hostCapabilities) || {};
      applyHostContext(result && result.hostContext);
      notify("ui/notifications/initialized", {});
      reportSize();
      return result;
    }).catch((err) => {
      if (onFail) onFail(err);
      throw err;
    });
  }

  return {
    connect,
    callTool,
    updateModelContext,
    sendMessage,
    requestDisplayMode,
    reportSize,
    hostSupports: (name) => Boolean(hostCapabilities[name]),
  };
})();

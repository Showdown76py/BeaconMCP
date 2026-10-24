"""HTML building blocks shared by the self-contained OAuth pages.

``/oauth/authorize`` must work with the dashboard disabled, so they don't use the dashboard's Jinja templates or its
``/app/static`` bundle. CSS and JS live here as plain strings rather than in
f-strings: they are brace-dense and doubling every brace made them unreadable.
"""

from __future__ import annotations

import html

AUTH_CSS = """
:root {
  --accent: oklch(0.68 0.17 48);
  --accent-soft: oklch(0.68 0.17 48 / 0.12);
  --accent-softer: oklch(0.68 0.17 48 / 0.06);
  --accent-border: oklch(0.68 0.17 48 / 0.35);
  --accent-hover: oklch(0.62 0.18 48);
  --accent-fg: #fff;
  --bg: oklch(0.99 0.004 70);
  --bg-soft: oklch(0.975 0.005 70);
  --bg-elev: #fff;
  --fg: oklch(0.22 0.01 70);
  --fg-mid: oklch(0.42 0.008 70);
  --fg-muted: oklch(0.55 0.008 70);
  --fg-faint: oklch(0.7 0.006 70);
  --border: oklch(0.92 0.006 70);
  --border-strong: oklch(0.86 0.008 70);
  --border-subtle: oklch(0.95 0.005 70);
  --danger: oklch(0.58 0.19 25);
  --danger-soft: oklch(0.58 0.19 25 / 0.1);
  --success: oklch(0.62 0.12 155);
  --success-soft: oklch(0.62 0.12 155 / 0.12);
  --shadow: 0 1px 2px rgba(20,14,8,0.04), 0 4px 20px rgba(20,14,8,0.06);
  --font: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
}
[data-theme="dark"] {
  --bg: oklch(0.16 0.008 60);
  --bg-soft: oklch(0.19 0.008 60);
  --bg-elev: oklch(0.21 0.009 60);
  --fg: oklch(0.95 0.006 70);
  --fg-mid: oklch(0.78 0.008 70);
  --fg-muted: oklch(0.62 0.01 70);
  --fg-faint: oklch(0.45 0.008 70);
  --border: oklch(0.28 0.009 60);
  --border-strong: oklch(0.36 0.01 60);
  --border-subtle: oklch(0.24 0.008 60);
  --accent: oklch(0.75 0.17 50);
  --accent-soft: oklch(0.75 0.17 50 / 0.16);
  --accent-softer: oklch(0.75 0.17 50 / 0.08);
  --accent-border: oklch(0.75 0.17 50 / 0.4);
  --accent-hover: oklch(0.82 0.17 50);
  --accent-fg: oklch(0.12 0.008 60);
  --danger: oklch(0.68 0.19 25);
  --success: oklch(0.72 0.14 155);
  --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 4px 20px rgba(0,0,0,0.4);
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  font-family: var(--font);
  font-size: 15px;
  color: var(--fg);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
}
body {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
}
.auth-card {
  width: 100%; max-width: 400px;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: 18px;
  overflow: hidden;
  box-shadow: var(--shadow);
  animation: rise 400ms var(--ease-out) both;
}
.auth-body { padding: 26px 28px 24px; }
.server-bar {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 11px 18px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-soft);
  font-size: 12px; color: var(--fg-muted);
}
.server-bar .brand { display: inline-flex; align-items: center; gap: 7px; font-weight: 600; color: var(--fg); font-size: 13px; }
.server-bar .host { display: inline-flex; align-items: center; gap: 6px; font-family: var(--font-mono); color: var(--fg-mid); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.logo-sm { width: 18px; height: 18px; border-radius: 5px; display: block; flex-shrink: 0; }
.pair { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; }
.app-icon {
  width: 40px; height: 40px; border-radius: 11px; overflow: hidden;
  display: grid; place-items: center; flex-shrink: 0;
}
.app-icon.client { background: #fff; border: 1px solid var(--border); }
[data-theme="dark"] .app-icon.client { background: oklch(0.24 0.01 60); }
.app-icon.client img { width: 26px; height: 26px; object-fit: contain; display: block; }
.pair .dots { flex: 0 0 28px; height: 2px; background: repeating-linear-gradient(90deg, var(--border-strong) 0 4px, transparent 4px 8px); }
.perms { list-style: none; padding: 0; margin: 0 0 18px; border: 1px solid var(--border); border-radius: 12px; }
.perms li { display: flex; gap: 10px; padding: 10px 12px; font-size: 13px; line-height: 1.4; }
.perms li + li { border-top: 1px solid var(--border-subtle); }
.perms .ic { flex: 0 0 20px; height: 20px; border-radius: 6px; display: grid; place-items: center; background: var(--bg-soft); color: var(--fg-mid); }
.perms .ic.hot { background: var(--accent-soft); color: var(--accent); }
.perms small { display: block; color: var(--fg-muted); font-size: 12px; margin-top: 1px; }
.foot .row { display: flex; justify-content: space-between; gap: 10px; padding: 3px 0; }
.foot b { color: var(--fg); font-weight: 550; font-family: var(--font-mono); font-size: 11.5px; text-align: right; overflow-wrap: anywhere; }
.foot { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--border-subtle); font-size: 12px; color: var(--fg-muted); line-height: 1.5; }
.foot .url { font-family: var(--font-mono); font-size: 11.5px; color: var(--fg-muted); text-align: right; overflow-wrap: anywhere; }
.foot .url strong { color: var(--fg); font-weight: 700; }
@media (prefers-reduced-motion: reduce) { .auth-card { animation: none; } }
@keyframes rise {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
h1 { margin: 0 0 4px; font-size: 22px; font-weight: 600; letter-spacing: -0.015em; }
.sub { margin: 0 0 18px; font-size: 13.5px; color: var(--fg-muted); }
.sub strong { color: var(--fg); font-weight: 600; }
.banner {
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 13px;
  margin: 0 0 14px;
  background: var(--danger-soft);
  color: var(--danger);
  border: 1px solid color-mix(in oklab, var(--danger) 35%, var(--border));
}
.banner-success {
  background: var(--success-soft);
  color: var(--success);
  border-color: color-mix(in oklab, var(--success) 35%, var(--border));
}
.toast-banner {
  background: var(--accent-softer);
  border: 1px solid var(--accent-border);
  color: var(--fg);
  border-radius: 10px;
  padding: 9px 12px;
  font-size: 12.5px;
  margin-bottom: 16px;
  display: flex; align-items: center; gap: 8px;
}
.toast-banner .dot {
  width: 6px; height: 6px;
  border-radius: 50%; background: var(--accent);
  flex-shrink: 0;
}
.toast-banner b { font-family: var(--font-mono); margin-left: 2px; }
.totp-inputs {
  display: flex; gap: 8px; justify-content: space-between;
  margin: 8px 0 18px;
}
.totp-inputs input {
  width: 100%; aspect-ratio: 1 / 1.15;
  text-align: center;
  font-size: 24px; font-weight: 600;
  font-family: var(--font-mono);
  background: var(--bg-soft);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  color: var(--fg);
  outline: none;
  transition: border-color 160ms var(--ease-out), box-shadow 160ms var(--ease-out);
}
.totp-inputs input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);
}
.totp-inputs input.filled {
  background: var(--accent-softer);
  border-color: var(--accent-border);
}
.btn-primary {
  width: 100%;
  padding: 12px 16px;
  border: 0; border-radius: 10px;
  background: var(--accent); color: var(--accent-fg);
  font-family: var(--font); font-weight: 600; font-size: 14.5px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  transition: background 180ms var(--ease-out), transform 100ms var(--ease-out);
  box-shadow: 0 4px 14px oklch(0.68 0.17 48 / 0.28), inset 0 1px 0 rgba(255,255,255,0.2);
}
.btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
.btn-primary:active:not(:disabled) { transform: translateY(1px); }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }
.btn-ghost {
  width: 100%;
  padding: 11px 16px;
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  background: var(--bg-soft);
  color: var(--fg);
  font-family: var(--font); font-weight: 550; font-size: 14px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  transition: border-color 160ms var(--ease-out), background 160ms var(--ease-out);
}
.btn-ghost:hover:not(:disabled) {
  border-color: var(--accent-border);
  background: var(--accent-softer);
}
.btn-ghost:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-ghost svg { flex-shrink: 0; }
.btn-primary.is-loading, .btn-ghost.is-loading {
  position: relative; overflow: hidden; cursor: progress; opacity: 1;
}
.btn-primary.is-loading::after, .btn-ghost.is-loading::after {
  content: "";
  position: absolute; inset: 0;
  background: linear-gradient(100deg, transparent 20%, rgba(255,255,255,0.38) 50%, transparent 80%);
  transform: translateX(-100%);
  animation: shimmer-sweep 1150ms var(--ease-out) infinite;
  pointer-events: none;
}
.btn-ghost.is-loading::after {
  background: linear-gradient(100deg, transparent 20%, var(--accent-soft) 50%, transparent 80%);
}
@keyframes shimmer-sweep { to { transform: translateX(100%); } }
.btn-primary.is-loading .btn-icon { opacity: 0; }
@media (prefers-reduced-motion: reduce) {
  .btn-primary.is-loading::after, .btn-ghost.is-loading::after {
    animation: none; opacity: 0.25;
  }
}
.alt-divider {
  display: flex; align-items: center; gap: 12px;
  margin: 18px 0 14px;
  color: var(--fg-faint);
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
}
.alt-divider::before, .alt-divider::after {
  content: ""; flex: 1; height: 1px; background: var(--border);
}
.hint {
  margin: 10px 0 0;
  font-size: 12.5px; line-height: 1.5;
  color: var(--fg-muted); text-align: center;
}
.success-mark {
  width: 42px; height: 42px;
  border-radius: 50%;
  display: grid; place-items: center;
  margin-bottom: 16px;
  color: var(--success);
  background: var(--success-soft);
  border: 1px solid color-mix(in oklab, var(--success) 35%, var(--border));
  animation: pop-in 320ms var(--ease-out) both;
}
@keyframes pop-in {
  from { opacity: 0; transform: scale(0.82); }
  to { opacity: 1; transform: scale(1); }
}
.expiry-card {
  margin: 4px 0 0;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-soft);
}
.expiry-row {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 12px; padding: 5px 0;
}
.expiry-row + .expiry-row { border-top: 1px solid var(--border-subtle); }
.expiry-row dt { font-size: 12.5px; color: var(--fg-muted); }
.expiry-row dd {
  margin: 0; font-size: 13px; font-weight: 550;
  color: var(--fg); text-align: right;
}
#passkey-add-block { margin-top: 18px; }
#finalize-form { margin-top: 20px; }
"""


THEME_JS = """
(function() {
  try {
    var raw = localStorage.getItem("beaconmcp-ui-state");
    var s = raw ? JSON.parse(raw) : {};
    var t = s.theme || "auto";
    var dark = t === "dark" || (t === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  } catch (e) {}
})();
"""


# Self-contained: /oauth/authorize is reachable even when the dashboard (and
# its /app/static bundle) is disabled, so the WebAuthn plumbing is duplicated
# here rather than imported from static/webauthn.js.
AUTHORIZE_JS = """
(function() {
  "use strict";

  var card = document.querySelector(".auth-card");
  var stepVerify = document.getElementById("step-verify");
  var stepDone = document.getElementById("step-done");
  var form = document.getElementById("authorize-form");
  var totpHidden = document.getElementById("totp");
  var verifyBtn = document.getElementById("verify-btn");
  var container = document.getElementById("totp-inputs");
  var inputs = container ? container.querySelectorAll("input") : [];
  var verifyError = document.getElementById("verify-error");
  var doneError = document.getElementById("done-error");
  var doneOk = document.getElementById("done-ok");
  var passkeyBlock = document.getElementById("passkey-block");
  var passkeyBtn = document.getElementById("passkey-btn");
  var passkeyAddBlock = document.getElementById("passkey-add-block");
  var addPasskeyBtn = document.getElementById("add-passkey-btn");
  var ticketInput = document.getElementById("ticket");
  var finalizeForm = document.getElementById("finalize-form");
  var finishBtn = document.getElementById("finish-btn");

  var passkeysEnabled = card && card.dataset.passkeys === "true";
  var clientId = card ? card.dataset.clientId : "";
  var busy = false;

  function b64urlToBuf(value) {
    var s = String(value).replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var bin = window.atob(s);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }
  function bufToB64url(buf) {
    var bytes = new Uint8Array(buf), bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return window.btoa(bin).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
  }
  function descriptors(list) {
    return (list || []).map(function(d) {
      var out = { type: d.type || "public-key", id: b64urlToBuf(d.id) };
      if (d.transports && d.transports.length) out.transports = d.transports;
      return out;
    });
  }
  function webauthnSupported() {
    return !!(window.PublicKeyCredential && navigator.credentials &&
              navigator.credentials.create && navigator.credentials.get);
  }
  function describeError(err) {
    if (!err) return "Passkey request failed.";
    if (err.name === "NotAllowedError") return "Passkey prompt cancelled or timed out.";
    if (err.name === "InvalidStateError") return "This device already has a passkey for this client.";
    if (err.name === "SecurityError") return "Passkeys need a secure origin (HTTPS or localhost).";
    return err.message || String(err);
  }
  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-BeaconMCP-Mode": "json" },
      body: JSON.stringify(body || {})
    }).then(function(res) {
      return res.json().catch(function() { return {}; }).then(function(data) {
        return { ok: res.ok, data: data };
      });
    });
  }
  function show(el, message) {
    if (!el) return;
    el.textContent = message || "";
    el.hidden = !message;
  }
  function setLoading(btn, loading, label) {
    if (!btn) return;
    var labelEl = btn.querySelector(".btn-label");
    if (loading) {
      if (labelEl && label) {
        if (!btn.dataset.idleLabel) btn.dataset.idleLabel = labelEl.textContent;
        labelEl.textContent = label;
      }
      btn.classList.add("is-loading");
      btn.disabled = true;
    } else {
      if (labelEl && btn.dataset.idleLabel) {
        labelEl.textContent = btn.dataset.idleLabel;
        delete btn.dataset.idleLabel;
      }
      btn.classList.remove("is-loading");
    }
  }
  function formatDateTime(epochSeconds) {
    if (!epochSeconds || !isFinite(epochSeconds)) return "\\u2014";
    var d = new Date(epochSeconds * 1000);
    var sameDay = d.toDateString() === new Date().toDateString();
    var time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    if (sameDay) return "today at " + time;
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) + " at " + time;
  }
  function formatRelative(epochSeconds) {
    var secs = epochSeconds - (Date.now() / 1000);
    if (secs <= 0) return "expired";
    var hours = secs / 3600;
    if (hours < 1) return "in " + Math.max(1, Math.round(secs / 60)) + " min";
    if (hours < 48) return "in " + Math.round(hours) + " h";
    return "in " + Math.round(hours / 24) + " days";
  }

  // --- TOTP boxes ---
  function collectTotp() {
    var s = "";
    inputs.forEach(function(i) { s += (i.value || "").replace(/\\D/g, ""); });
    return s;
  }
  function refresh() {
    var v = collectTotp();
    if (totpHidden) totpHidden.value = v;
    if (verifyBtn) verifyBtn.disabled = busy || v.length !== 6;
  }
  function clearTotp() {
    inputs.forEach(function(i) { i.value = ""; i.classList.remove("filled"); });
    refresh();
    if (inputs[0]) inputs[0].focus();
  }

  inputs.forEach(function(inp, i) {
    inp.addEventListener("input", function(e) {
      var v = (e.target.value || "").replace(/\\D/g, "");
      // autofill / one-time-code suggestions drop the whole code in one box
      if (v.length > 1) {
        v.slice(0, inputs.length - i).split("").forEach(function(ch, k) {
          inputs[i + k].value = ch; inputs[i + k].classList.add("filled");
        });
        v = v.slice(0, 1);
      }
      e.target.value = v.slice(0, 1);
      if (v) {
        e.target.classList.add("filled");
        if (inputs[i + 1]) inputs[i + 1].focus();
      } else {
        e.target.classList.remove("filled");
      }
      refresh();
    });
    inp.addEventListener("keydown", function(e) {
      if (e.key === "Enter") {
        e.preventDefault();
        refresh();
        if (!busy && collectTotp().length === 6) submitTotp();
        return;
      }
      if (e.key === "Backspace" && !e.target.value && inputs[i - 1]) {
        inputs[i - 1].focus();
        inputs[i - 1].value = "";
        inputs[i - 1].classList.remove("filled");
        refresh();
      }
    });
    inp.addEventListener("paste", function(e) {
      e.preventDefault();
      var src = e.clipboardData || window.clipboardData;
      var pasted = ((src && src.getData("text")) || "").replace(/\\D/g, "").slice(0, 6);
      pasted.split("").forEach(function(ch, k) {
        if (inputs[k]) { inputs[k].value = ch; inputs[k].classList.add("filled"); }
      });
      if (inputs[Math.min(pasted.length, 5)]) inputs[Math.min(pasted.length, 5)].focus();
      refresh();
    });
  });

  // --- approved screen ---
  function showDone(payload) {
    if (ticketInput) ticketInput.value = payload.ticket || "";
    var accessEl = document.getElementById("access-expiry");
    if (accessEl) {
      accessEl.textContent = formatDateTime(payload.access_expires_at) +
        " (" + formatRelative(payload.access_expires_at) + ")";
    }
    var ticketEl = document.getElementById("ticket-expiry");
    if (ticketEl) ticketEl.textContent = formatRelative(payload.ticket_expires_at);
    if (passkeyAddBlock) {
      var canAdd = payload.passkeys_enabled && webauthnSupported();
      passkeyAddBlock.hidden = !canAdd;
      // Server says passkeys are on but the browser won't expose the API:
      // that is an insecure origin, and silently hiding the button just
      // makes people wonder where it went.
      var addNote = document.getElementById("passkey-add-unsupported");
      if (addNote) addNote.hidden = canAdd || !payload.passkeys_enabled;
    }
    if (stepVerify) stepVerify.hidden = true;
    if (stepDone) stepDone.hidden = false;
    if (finishBtn) finishBtn.focus();
  }

  function submitTotp() {
    if (busy || !form) return;
    var code = collectTotp();
    if (code.length !== 6) return;
    busy = true;
    show(verifyError, "");
    setLoading(verifyBtn, true, "Verifying\\u2026");

    var body = new URLSearchParams(new FormData(form));
    body.set("totp", code);
    fetch("/oauth/authorize", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-BeaconMCP-Mode": "json"
      },
      body: body.toString()
    }).then(function(res) {
      return res.json().catch(function() { return {}; }).then(function(data) {
        return { ok: res.ok, data: data };
      });
    }).then(function(res) {
      busy = false;
      setLoading(verifyBtn, false);
      if (res.ok && res.data && res.data.ok) { showDone(res.data); return; }
      show(verifyError, (res.data && res.data.error) || "Authorization failed.");
      clearTotp();
    }).catch(function() {
      busy = false;
      setLoading(verifyBtn, false);
      show(verifyError, "Network error. Try again.");
      refresh();
    });
  }

  if (form) {
    form.addEventListener("submit", function(e) {
      e.preventDefault();
      submitTotp();
    });
  }

  // --- passkey instead of a code ---
  if (passkeyBlock) {
    var canUse = passkeysEnabled && webauthnSupported();
    passkeyBlock.hidden = !canUse;
    var note = document.getElementById("passkey-unsupported");
    if (note) note.hidden = canUse || !passkeysEnabled;
  }

  if (passkeyBtn) {
    passkeyBtn.addEventListener("click", function() {
      if (busy || !form) return;
      busy = true;
      show(verifyError, "");
      setLoading(passkeyBtn, true, "Waiting for your passkey\\u2026");
      if (verifyBtn) verifyBtn.disabled = true;

      var params = {};
      new FormData(form).forEach(function(v, k) { params[k] = v; });
      postJson("/oauth/passkey/options", { client_id: clientId }).then(function(res) {
        if (!res.ok || !res.data.ok) {
          throw new Error(res.data.error || "Passkey sign-in unavailable.");
        }
        var options = Object.assign({}, res.data.options);
        options.challenge = b64urlToBuf(res.data.options.challenge);
        options.allowCredentials = descriptors(res.data.options.allowCredentials);
        return navigator.credentials.get({ publicKey: options }).then(function(cred) {
          if (!cred) throw new Error("No passkey was selected.");
          return postJson("/oauth/passkey/verify", {
            state: res.data.state,
            params: params,
            credential: {
              id: cred.id,
              rawId: bufToB64url(cred.rawId),
              type: cred.type,
              clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
              response: {
                clientDataJSON: bufToB64url(cred.response.clientDataJSON),
                authenticatorData: bufToB64url(cred.response.authenticatorData),
                signature: bufToB64url(cred.response.signature),
                userHandle: cred.response.userHandle ? bufToB64url(cred.response.userHandle) : null
              }
            }
          });
        });
      }).then(function(res) {
        busy = false;
        setLoading(passkeyBtn, false);
        refresh();
        if (res.ok && res.data && res.data.ok) { showDone(res.data); return; }
        show(verifyError, (res.data && res.data.error) || "Passkey rejected.");
      }).catch(function(err) {
        busy = false;
        setLoading(passkeyBtn, false);
        refresh();
        show(verifyError, describeError(err));
      });
    });
  }

  // --- enrol a passkey right after approving ---
  if (addPasskeyBtn) {
    addPasskeyBtn.addEventListener("click", function() {
      show(doneError, "");
      show(doneOk, "");
      setLoading(addPasskeyBtn, true, "Follow your device prompt\\u2026");
      var ticket = ticketInput ? ticketInput.value : "";

      postJson("/oauth/passkey/register/options", { ticket: ticket }).then(function(res) {
        if (!res.ok || !res.data.ok) {
          throw new Error(res.data.error || "Could not start registration.");
        }
        var options = Object.assign({}, res.data.options);
        options.challenge = b64urlToBuf(res.data.options.challenge);
        options.user = Object.assign({}, res.data.options.user, {
          id: b64urlToBuf(res.data.options.user.id)
        });
        options.excludeCredentials = descriptors(res.data.options.excludeCredentials);
        return navigator.credentials.create({ publicKey: options }).then(function(cred) {
          if (!cred) throw new Error("No credential was created.");
          var payload = {
            id: cred.id,
            rawId: bufToB64url(cred.rawId),
            type: cred.type,
            clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
            response: {
              clientDataJSON: bufToB64url(cred.response.clientDataJSON),
              attestationObject: bufToB64url(cred.response.attestationObject)
            }
          };
          if (cred.response.getTransports) {
            try { payload.response.transports = cred.response.getTransports(); } catch (e) {}
          }
          return postJson("/oauth/passkey/register/verify", {
            ticket: ticket, state: res.data.state, credential: payload
          });
        });
      }).then(function(res) {
        setLoading(addPasskeyBtn, false);
        if (res.ok && res.data && res.data.ok) {
          show(doneOk, "Passkey \\u201c" + res.data.passkey.label +
               "\\u201d registered. Next time you can skip the 2FA code.");
          addPasskeyBtn.disabled = true;
          var hint = document.getElementById("passkey-add-hint");
          if (hint) hint.hidden = true;
          return;
        }
        show(doneError, (res.data && res.data.error) || "Registration failed.");
      }).catch(function(err) {
        setLoading(addPasskeyBtn, false);
        show(doneError, describeError(err));
      });
    });
  }

  if (finalizeForm) {
    finalizeForm.addEventListener("submit", function() {
      setLoading(finishBtn, true, "Redirecting\\u2026");
    });
  }

  if (inputs[0] && !inputs[0].disabled && stepDone && stepDone.hidden) inputs[0].focus();
})();
"""


# vector redraw of assets/logo.webp, inlined so the pages need no static route
LOGO_SYMBOL = """<svg width="0" height="0" style="position:absolute" aria-hidden="true">
<symbol id="bmcp-logo" viewBox="0 0 512 512">
<rect width="512" height="512" fill="#FF8124"/>
<g fill="#fff"><rect x="107" y="110" width="298" height="84" rx="12"/><rect x="107" y="212" width="298" height="84" rx="12"/><rect x="107" y="314" width="298" height="84" rx="12"/></g>
<path stroke="#FF8124" stroke-width="4" stroke-linecap="round" d="M128 145h48M128 162h48M128 247h48M128 264h48M128 349h48M128 366h48"/>
<g fill="#FF8124"><circle cx="357" cy="155" r="13"/><circle cx="357" cy="255" r="13"/><circle cx="357" cy="357" r="13"/></g>
</symbol>
</svg>"""

_LOCK_ICON = (
    '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><rect x="4" y="11" '
    'width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>'
)

PASSKEY_ICON = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="10" cy="8" r="4"/><path d="M10.9 13.1A6 6 0 0 0 4 19v1h7"/>'
    '<path d="M17.5 12a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5z"/>'
    '<path d="M17.5 17v4l1.5-1.2 1.5 1.2v-4"/></svg>'
)

CHECK_ICON = (
    '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M5 12l5 5L20 7"/></svg>'
)


def logo_svg(size: int, cls: str = "") -> str:
    klass = f' class="{cls}"' if cls else ""
    return (
        f'<svg{klass} width="{size}" height="{size}" role="img" aria-label="BeaconMCP">'
        '<use href="#bmcp-logo"/></svg>'
    )


def server_bar(host: str) -> str:
    return (
        '<div class="server-bar">'
        f'<span class="brand">{logo_svg(18, "logo-sm")}BeaconMCP</span>'
        f'<span class="host" title="{html.escape(host)}">{_LOCK_ICON}{html.escape(host)}</span>'
        "</div>"
    )


def client_pair(logo_url: str, client_name: str) -> str:
    return (
        '<div class="pair">'
        f'<div class="app-icon client"><img src="{html.escape(logo_url)}" '
        f'alt="{html.escape(client_name)}" width="26" height="26"></div>'
        '<div class="dots" aria-hidden="true"></div>'
        f'<div class="app-icon">{logo_svg(40)}</div>'
        "</div>"
    )


def totp_boxes(disabled: bool = False) -> str:
    dis = " disabled" if disabled else ""
    return '<div class="totp-inputs" id="totp-inputs">' + "".join(
        f'<input maxlength="6" inputmode="numeric" autocomplete="one-time-code" '
        f'aria-label="Digit {i}"{dis}>'
        for i in range(1, 7)
    ) + "</div>"


# the auth pages must never render inside someone else's frame (clickjacking
# the TOTP or passkey step)
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def page(title: str, body: str, *, script: str = "") -> str:
    """Full HTML document around ``body`` (already escaped by the caller)."""
    tail = f"<script>{script}</script>" if script else ""
    return f"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;450;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{AUTH_CSS}</style>
<script>{THEME_JS}</script>
</head>
<body>
{LOGO_SYMBOL}
{body}
{tail}
</body>
</html>
"""




_ICON_EYE = (
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 '
    '7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>'
)
_ICON_BOLT = (
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round"><path d="M13 2 3 14h9l-1 8 10-12h-9z"/></svg>'
)
_ICON_POWER = (
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 '
    '1 1-12.8 0"/></svg>'
)
_ICON_TERM = (
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round"><path d="M4 7l5 5-5 5M12 19h8"/></svg>'
)


def capability_kinds(*, proxmox: bool, bmc: bool, ssh: bool) -> list[str]:
    return [k for k, on in (("Proxmox", proxmox), ("BMC", bmc), ("SSH", ssh)) if on]


def permissions_list(kinds: list[str]) -> str:
    """What access grants, by capability kind only: no counts or hostnames,
    the page is reachable before 2FA."""
    def item(icon: str, hot: bool, title: str, note: str) -> str:
        cls = "ic hot" if hot else "ic"
        return (
            f'<li><span class="{cls}">{icon}</span>'
            f"<div>{title}<small>{note}</small></div></li>"
        )

    rows = [item(_ICON_EYE, False, "Read infrastructure status", " · ".join(kinds) or "—")]
    if "Proxmox" in kinds:
        rows.append(item(_ICON_BOLT, True, "Start, stop, migrate VMs and run commands",
                         "Logged in the audit trail"))
    elif "SSH" in kinds:
        rows.append(item(_ICON_TERM, True, "Run shell commands on your hosts",
                         "Logged in the audit trail"))
    if "BMC" in kinds:
        rows.append(item(_ICON_POWER, True, "Power off or reset physical servers",
                         "Confirmed in your client when it supports it"))
    return '<ul class="perms">' + "".join(rows) + "</ul>"


def redirect_display(redirect_uri: str) -> str:
    """Redirect URI with its host in bold, the part people should check."""
    from urllib.parse import urlsplit

    parts = urlsplit(redirect_uri)
    host = parts.hostname or ""
    if not host or not parts.scheme:
        return f'<span class="url">{html.escape(redirect_uri)}</span>'
    netloc = parts.netloc
    i = netloc.lower().rfind(host.lower())
    before, after = netloc[:i], netloc[i + len(host):]
    tail = parts.path + (f"?{parts.query}" if parts.query else "")
    return (
        f'<span class="url">{html.escape(parts.scheme)}://{html.escape(before)}'
        f"<strong>{html.escape(netloc[i:i + len(host)])}</strong>"
        f"{html.escape(after)}{html.escape(tail)}</span>"
    )


def render_authorize_page(
    *, host: str, client_id: str, client_name: str, logo_url: str,
    redirect_uri: str, kinds: list[str], hidden_inputs: str, banner: str,
    locked: bool, approved: bool, passkeys_on: bool, ticket: str,
    access_expiry: str, ticket_expiry: str,
) -> str:
    """``/oauth/authorize``: connect step, then the post-2FA confirmation.

    ``hidden_inputs`` and ``banner`` are pre-escaped HTML; ``access_expiry``
    and ``ticket_expiry`` too. Element ids are what ``AUTHORIZE_JS`` binds to.
    """
    e = html.escape
    disabled = " disabled" if locked else ""
    name = e(client_name)
    body = f"""<div class="auth-card"
     data-passkeys="{'true' if passkeys_on else 'false'}"
     data-client-id="{e(client_id)}">
  {server_bar(host)}
  <div class="auth-body">
  {client_pair(logo_url, client_name)}

  <section id="step-verify"{' hidden' if approved else ''}>
    <h1>Connect {name}</h1>
    <p class="sub"><strong>{name}</strong> wants to manage your infrastructure through this BeaconMCP server.</p>
    {banner}
    {permissions_list(kinds)}
    <div class="banner" id="verify-error" hidden></div>
    <div id="passkey-block" hidden>
      <button type="button" class="btn-primary" id="passkey-btn"{disabled}>
        {PASSKEY_ICON}<span class="btn-label">Continue with passkey</span>
      </button>
      <div class="alt-divider"><span>or 6-digit code</span></div>
    </div>
    <form method="POST" action="/oauth/authorize" id="authorize-form">
{hidden_inputs}
      {totp_boxes(locked)}
      <input type="hidden" name="totp" id="totp" pattern="\\d{{6}}" required>
      <button type="submit" class="btn-primary" id="verify-btn"{disabled} disabled>
        <span class="btn-label">Verify and authorize</span>
        <svg class="btn-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>
      </button>
    </form>
    <p class="hint" id="passkey-unsupported" hidden>
      This browser can't use passkeys here &mdash; they require HTTPS (or a
      localhost address).
    </p>
    <div class="foot">
      <div class="row"><span>Redirects to</span>{redirect_display(redirect_uri)}</div>
      <div class="row"><span>Client</span><b>{e(client_id)}</b></div>
    </div>
  </section>

  <section id="step-done"{'' if approved else ' hidden'}>
    <div class="success-mark" aria-hidden="true">{CHECK_ICON}</div>
    <h1>Two-factor confirmed</h1>
    <p class="sub">You're about to grant <strong>{name}</strong> access to this BeaconMCP server.</p>
    <div class="banner" id="done-error" hidden></div>
    <div class="banner banner-success" id="done-ok" hidden></div>
    <dl class="expiry-card">
      <div class="expiry-row"><dt>Access expires</dt><dd id="access-expiry">{access_expiry}</dd></div>
      <div class="expiry-row"><dt>This approval expires</dt><dd id="ticket-expiry">{ticket_expiry}</dd></div>
    </dl>
    <p class="hint">The client will have to go through two-factor again once its access expires.</p>
    <div id="passkey-add-block" hidden>
      <button type="button" class="btn-ghost" id="add-passkey-btn">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="8" r="4"/><path d="M10.9 13.1A6 6 0 0 0 4 19v1h7"/><path d="M18 13v6M15 16h6"/></svg>
        <span class="btn-label">Add a passkey</span>
      </button>
      <p class="hint" id="passkey-add-hint">Register this device so next time you can skip the 2FA code.</p>
    </div>
    <p class="hint" id="passkey-add-unsupported" hidden>
      This browser can't register a passkey here &mdash; they require HTTPS
      (or a localhost address).
    </p>
    <form method="POST" action="/oauth/authorize/finalize" id="finalize-form">
      <input type="hidden" name="ticket" id="ticket" value="{e(ticket)}">
      <button type="submit" class="btn-primary" id="finish-btn">
        <span class="btn-label">Finish signing in</span>
        <svg class="btn-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
      </button>
    </form>
  </section>
  </div>
</div>"""
    return page("BeaconMCP · Authorize", body, script=AUTHORIZE_JS)

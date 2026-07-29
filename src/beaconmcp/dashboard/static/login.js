// Three-step login.
//
//   1. Client ID / Secret
//   2. TOTP code, or a passkey instead
//   3. Signed in: session lifetime, optional passkey enrolment, finish
//
// Step 3 only exists when JavaScript is on: the form still posts normally
// and gets a 303 to the landing page when it isn't, so the whole passkey
// layer is progressive enhancement over a flow that already worked.
(function() {
  "use strict";

  var form = document.getElementById("login-form");
  if (!form) return;

  var card = document.querySelector(".auth-card");
  var s1 = document.getElementById("step-1");
  var s2 = document.getElementById("step-2");
  var s3 = document.getElementById("step-3");
  var toggle = document.getElementById("toggle-pw");
  var pw = document.getElementById("client_secret");
  var cid = document.getElementById("client_id");
  var toStep2 = document.getElementById("to-step-2");
  var back = document.getElementById("back-to-1");
  var totpHidden = document.getElementById("totp");
  var verifyBtn = document.getElementById("verify-btn");
  var inputs = document.querySelectorAll("#totp-inputs input");
  var verifiedLabel = document.getElementById("client-verified");
  var csrfInput = document.getElementById("csrf-token");
  var step2Error = document.getElementById("step-2-error");
  var step3Error = document.getElementById("step-3-error");
  var step3Ok = document.getElementById("step-3-ok");
  var passkeyLoginBlock = document.getElementById("passkey-login-block");
  var passkeyLoginBtn = document.getElementById("passkey-login-btn");
  var passkeyAddBlock = document.getElementById("passkey-add-block");
  var addPasskeyBtn = document.getElementById("add-passkey-btn");
  var finishBtn = document.getElementById("finish-btn");

  var passkeysEnabled = card && card.dataset.passkeysEnabled === "true";
  var secureContext = card && card.dataset.secureContext === "true";
  var passkeysUsable = passkeysEnabled && secureContext &&
    window.BeaconPasskeys && window.BeaconPasskeys.supported();

  var session = null;   // payload returned by the successful login
  var busy = false;

  if (passkeyLoginBlock && !passkeysUsable) passkeyLoginBlock.hidden = true;

  // --- small helpers ----------------------------------------------------

  function csrf() {
    return csrfInput ? csrfInput.value : "";
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf(),
        "X-BeaconMCP-Mode": "json",
      },
      body: JSON.stringify(body || {}),
    }).then(function(res) {
      return res.json().catch(function() { return {}; }).then(function(data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    });
  }

  function showError(el, message) {
    if (!el) return;
    el.textContent = message;
    el.hidden = !message;
  }

  function showOk(el, message) {
    if (!el) return;
    el.textContent = message;
    el.hidden = !message;
  }

  // Shimmer: the button keeps its width, gains a sweeping highlight and
  // swaps its label. Kept as a class so the CSS owns the animation.
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
    if (!epochSeconds || !isFinite(epochSeconds)) return "—";
    var d = new Date(epochSeconds * 1000);
    var today = new Date();
    var sameDay = d.toDateString() === today.toDateString();
    var time = d.toLocaleTimeString(undefined, {
      hour: "2-digit", minute: "2-digit",
    });
    if (sameDay) return "today at " + time;
    return d.toLocaleDateString(undefined, {
      day: "numeric", month: "short", year: "numeric",
    }) + " at " + time;
  }

  function formatRelative(epochSeconds) {
    var secs = epochSeconds - (Date.now() / 1000);
    if (secs <= 0) return "expired";
    var hours = secs / 3600;
    if (hours < 1) return "in " + Math.max(1, Math.round(secs / 60)) + " min";
    if (hours < 48) return "in " + Math.round(hours) + " h";
    return "in " + Math.round(hours / 24) + " days";
  }

  // --- step 1 -> step 2 -------------------------------------------------

  if (toggle) {
    toggle.addEventListener("click", function() {
      pw.type = pw.type === "password" ? "text" : "password";
    });
  }

  function goStep2() {
    if (!cid.value.trim() || !pw.value) return;
    if (verifiedLabel) verifiedLabel.textContent = cid.value.trim();
    showError(step2Error, "");
    s1.hidden = true;
    s2.hidden = false;
    setTimeout(function() { if (inputs[0]) inputs[0].focus(); }, 40);
  }
  if (toStep2) toStep2.addEventListener("click", goStep2);

  // Enter in step-1 advances rather than submitting a half-filled form.
  [cid, pw].forEach(function(el) {
    if (!el) return;
    el.addEventListener("keydown", function(e) {
      if (e.key === "Enter") {
        e.preventDefault();
        goStep2();
      }
    });
  });

  if (back) back.addEventListener("click", function() {
    s2.hidden = true;
    s1.hidden = false;
  });

  // --- TOTP boxes -------------------------------------------------------

  function collectTotp() {
    var s = "";
    inputs.forEach(function(i) { s += (i.value || "").replace(/\D/g, ""); });
    return s;
  }

  function refresh() {
    var v = collectTotp();
    totpHidden.value = v;
    verifyBtn.disabled = busy || v.length !== 6;
  }

  inputs.forEach(function(inp, i) {
    inp.addEventListener("input", function(e) {
      var v = (e.target.value || "").replace(/\D/g, "");
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
      // Enter validates as soon as the six digits are in — no reaching
      // for the mouse after typing the last one.
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
      var pasted = ((src && src.getData("text")) || "").replace(/\D/g, "").slice(0, 6);
      pasted.split("").forEach(function(ch, k) {
        if (inputs[k]) { inputs[k].value = ch; inputs[k].classList.add("filled"); }
      });
      if (inputs[Math.min(pasted.length, 5)]) inputs[Math.min(pasted.length, 5)].focus();
      refresh();
    });
  });

  function clearTotp() {
    inputs.forEach(function(i) { i.value = ""; i.classList.remove("filled"); });
    totpHidden.value = "";
    refresh();
    if (inputs[0]) inputs[0].focus();
  }

  // --- step 3 -----------------------------------------------------------

  function showStep3(payload) {
    session = payload;
    // Signing in rotates the CSRF cookie; without picking the new value up
    // every follow-up fetch (passkey enrolment) would 403.
    if (payload.csrf_token && csrfInput) csrfInput.value = payload.csrf_token;
    s1.hidden = true;
    s2.hidden = true;
    s3.hidden = false;

    var who = document.getElementById("success-client");
    if (who) who.textContent = payload.client_name || payload.client_id || "—";

    var bearerEl = document.getElementById("bearer-expiry");
    if (bearerEl) {
      bearerEl.textContent = formatDateTime(payload.bearer_expires_at) +
        " (" + formatRelative(payload.bearer_expires_at) + ")";
    }
    var sessionEl = document.getElementById("session-expiry");
    if (sessionEl) sessionEl.textContent = formatDateTime(payload.session_expires_at);

    if (passkeyAddBlock) {
      var canAdd = payload.passkeys_enabled && payload.secure_context &&
        window.BeaconPasskeys && window.BeaconPasskeys.supported();
      passkeyAddBlock.hidden = !canAdd;
      var hint = document.getElementById("passkey-add-hint");
      if (canAdd && hint && payload.passkey_count > 0) {
        hint.textContent = payload.passkey_count === 1
          ? "1 passkey already registered. Add another for a second device."
          : payload.passkey_count + " passkeys already registered. " +
            "Add another for a second device.";
      }
    }
    if (finishBtn) finishBtn.focus();
  }

  function finish() {
    var next = (session && session.next) || "/app/tokens";
    setLoading(finishBtn, true, "Opening…");
    window.location.href = next;
  }

  if (finishBtn) finishBtn.addEventListener("click", finish);

  // --- TOTP submit ------------------------------------------------------

  function submitTotp() {
    if (busy) return;
    var code = collectTotp();
    if (code.length !== 6) return;
    busy = true;
    showError(step2Error, "");
    setLoading(verifyBtn, true, "Verifying…");

    var body = new URLSearchParams();
    body.set("csrf_token", csrf());
    body.set("client_id", cid.value.trim());
    body.set("client_secret", pw.value);
    body.set("totp", code);
    var nextField = form.querySelector('input[name="next"]');
    if (nextField) body.set("next", nextField.value);

    fetch("/app/login", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": csrf(),
        "X-BeaconMCP-Mode": "json",
      },
      body: body.toString(),
    }).then(function(res) {
      return res.json().catch(function() { return {}; }).then(function(data) {
        return { ok: res.ok, data: data };
      });
    }).then(function(res) {
      busy = false;
      setLoading(verifyBtn, false);
      if (res.ok && res.data && res.data.ok) {
        showStep3(res.data);
        return;
      }
      showError(step2Error, (res.data && res.data.error) || "Sign-in failed.");
      clearTotp();
    }).catch(function() {
      busy = false;
      setLoading(verifyBtn, false);
      showError(step2Error, "Network error. Try again.");
      refresh();
    });
  }

  form.addEventListener("submit", function(e) {
    e.preventDefault();
    if (totpHidden.value.length !== 6) {
      goStep2();
      return;
    }
    submitTotp();
  });

  // --- passkey sign-in (step 2) ----------------------------------------

  if (passkeyLoginBtn) {
    passkeyLoginBtn.addEventListener("click", function() {
      if (busy) return;
      var clientId = cid.value.trim();
      var secret = pw.value;
      if (!clientId || !secret) {
        showError(step2Error, "Enter your client credentials first.");
        return;
      }
      busy = true;
      showError(step2Error, "");
      setLoading(passkeyLoginBtn, true, "Waiting for your passkey…");
      verifyBtn.disabled = true;

      var nextField = form.querySelector('input[name="next"]');
      postJson("/app/api/passkeys/auth/options", {
        client_id: clientId,
        client_secret: secret,
      }).then(function(res) {
        if (!res.ok || !res.data.ok) {
          throw new Error(res.data.error || "Passkey sign-in unavailable.");
        }
        return window.BeaconPasskeys.authenticate(res.data.options)
          .then(function(assertion) {
            return postJson("/app/api/passkeys/auth/verify", {
              client_id: clientId,
              client_secret: secret,
              state: res.data.state,
              credential: assertion,
              next: nextField ? nextField.value : "",
            });
          });
      }).then(function(res) {
        busy = false;
        setLoading(passkeyLoginBtn, false);
        refresh();
        if (res.ok && res.data && res.data.ok) {
          showStep3(res.data);
          return;
        }
        showError(step2Error, (res.data && res.data.error) || "Passkey rejected.");
      }).catch(function(err) {
        busy = false;
        setLoading(passkeyLoginBtn, false);
        refresh();
        showError(step2Error, window.BeaconPasskeys.describeError(err));
      });
    });
  }

  // --- passkey enrolment (step 3) --------------------------------------

  if (addPasskeyBtn) {
    addPasskeyBtn.addEventListener("click", function() {
      showError(step3Error, "");
      showOk(step3Ok, "");
      setLoading(addPasskeyBtn, true, "Follow your device prompt…");

      postJson("/app/api/passkeys/register/options", {}).then(function(res) {
        if (!res.ok) throw new Error(res.data.error || "Could not start registration.");
        return window.BeaconPasskeys.register(res.data.options)
          .then(function(attestation) {
            return postJson("/app/api/passkeys/register/verify", {
              state: res.data.state,
              credential: attestation,
            });
          });
      }).then(function(res) {
        setLoading(addPasskeyBtn, false);
        if (res.ok && res.data && res.data.ok) {
          showOk(step3Ok, "Passkey “" + res.data.passkey.label +
                 "” registered. Next time you can skip the 2FA code.");
          addPasskeyBtn.disabled = true;
          var hint = document.getElementById("passkey-add-hint");
          if (hint) hint.hidden = true;
          return;
        }
        showError(step3Error, (res.data && res.data.error) || "Registration failed.");
      }).catch(function(err) {
        setLoading(addPasskeyBtn, false);
        showError(step3Error, window.BeaconPasskeys.describeError(err));
      });
    });
  }
})();

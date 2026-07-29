// "An update is available" toast, shown on every /app page to a signed-in
// operator. The endpoint is session-authenticated, so a 401 (login page,
// signed out) simply leaves the toast hidden -- no branching needed here.
(function() {
  "use strict";

  var root = document.getElementById("update-toast");
  if (!root) return;

  var DISMISS_KEY = "beaconmcp-update-dismissed";
  var state = null;

  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)beaconmcp_csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function dismissed(ref) {
    try {
      return window.localStorage.getItem(DISMISS_KEY) === ref;
    } catch (e) {
      return false;
    }
  }

  function remember(ref) {
    try {
      window.localStorage.setItem(DISMISS_KEY, ref);
    } catch (e) {}
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function plural(n, one, many) {
    return n + " " + (n === 1 ? one : many);
  }

  function render(data) {
    state = data;
    var commits = data.commits || [];
    var cfg = data.config || {};
    var newEnv = cfg.new_env_vars || [];
    var newKeys = cfg.new_config_keys || [];

    var html = '' +
      '<div class="ut-head">' +
        '<span class="ut-dot"></span>' +
        '<strong>Update available</strong>' +
        '<button type="button" class="ut-x" id="ut-close" aria-label="Dismiss">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>' +
        '</button>' +
      '</div>' +
      '<p class="ut-sub">' +
        esc(plural(data.behind, "commit", "commits")) + " behind " +
        '<code>' + esc(data.branch || "main") + '</code>' +
        (data.current_ref && data.latest_ref
          ? ' &middot; <code>' + esc(data.current_ref) + '</code> → <code>' + esc(data.latest_ref) + '</code>'
          : "") +
      '</p>';

    if (commits.length) {
      html += '<ul class="ut-log">';
      commits.slice(0, 4).forEach(function(c) {
        html += '<li><code>' + esc(c.sha) + '</code> ' + esc(c.subject) + '</li>';
      });
      if (commits.length > 4) {
        html += '<li class="ut-more">+ ' +
          esc(plural(commits.length - 4, "more commit", "more commits")) + '</li>';
      }
      html += '</ul>';
    }

    if (newEnv.length || newKeys.length) {
      html += '<div class="ut-warn"><strong>Needs configuration</strong>';
      if (newEnv.length) {
        html += '<div>New <code>.env</code> variables: ' +
          newEnv.map(function(v) { return '<code>' + esc(v) + '</code>'; }).join(", ") +
          '</div>';
      }
      if (newKeys.length) {
        html += '<div>New <code>beaconmcp.yaml</code> settings: ' +
          newKeys.slice(0, 6).map(function(v) { return '<code>' + esc(v) + '</code>'; }).join(", ") +
          (newKeys.length > 6 ? ", …" : "") +
          '</div>';
      }
      html += '</div>';
    }

    var steps = (data.instructions || []).join("\n");
    html += '<div class="ut-steps-head">' +
      'To update this ' + esc(data.install_kind) + ' install' +
      '<button type="button" class="ut-copy" id="ut-copy">Copy</button>' +
      '</div>' +
      '<pre class="ut-steps"><code>' + esc(steps) + '</code></pre>';

    if (data.blockers && data.blockers.length) {
      html += '<p class="ut-block">Automatic update unavailable: ' +
        esc(data.blockers.join("; ")) + '</p>';
    }

    html += '<div class="ut-actions">';
    if (data.can_self_update && data.self_update_allowed) {
      html += '<button type="button" class="ut-primary" id="ut-go">' +
        '<span class="btn-label">Update now</span></button>';
    }
    if (data.compare_url) {
      html += '<a class="ut-link" href="' + esc(data.compare_url) +
        '" target="_blank" rel="noopener">View changes</a>';
    }
    html += '</div>';
    html += '<div class="ut-result" id="ut-result" hidden></div>';

    root.innerHTML = html;
    root.hidden = false;

    var close = document.getElementById("ut-close");
    if (close) {
      close.addEventListener("click", function() {
        remember(data.latest_ref);
        root.hidden = true;
      });
    }
    var copy = document.getElementById("ut-copy");
    if (copy) {
      copy.addEventListener("click", function() {
        navigator.clipboard.writeText(steps).then(function() {
          copy.textContent = "Copied";
          setTimeout(function() { copy.textContent = "Copy"; }, 1600);
        }, function() {});
      });
    }
    var go = document.getElementById("ut-go");
    if (go) go.addEventListener("click", askForCode);
  }

  // Applying an update pulls code and restarts the process -- gated on a
  // fresh 2FA code, the same bar as minting a token.
  function askForCode() {
    var box = document.getElementById("ut-result");
    if (!box) return;
    box.hidden = false;
    box.innerHTML = '' +
      '<label class="ut-label" for="ut-totp">Confirm with your 2FA code</label>' +
      '<div class="ut-totp-row">' +
        '<input id="ut-totp" inputmode="numeric" maxlength="6" placeholder="000000" autocomplete="one-time-code">' +
        '<button type="button" class="ut-primary" id="ut-confirm">' +
          '<span class="btn-label">Update</span></button>' +
      '</div>' +
      '<p class="ut-note">Pulls the new code, reinstalls dependencies, ' +
      're-validates your config, then restarts. If the new code can\'t load ' +
      'your config it rolls back and does not restart.</p>';
    var input = document.getElementById("ut-totp");
    var confirm = document.getElementById("ut-confirm");
    if (input) input.focus();
    if (input) {
      input.addEventListener("keydown", function(e) {
        if (e.key === "Enter") { e.preventDefault(); run(); }
      });
    }
    if (confirm) confirm.addEventListener("click", run);
  }

  function run() {
    var input = document.getElementById("ut-totp");
    var confirm = document.getElementById("ut-confirm");
    var box = document.getElementById("ut-result");
    var code = input ? (input.value || "").replace(/\D/g, "") : "";
    if (code.length !== 6) {
      if (input) input.focus();
      return;
    }
    if (confirm) {
      confirm.classList.add("is-loading");
      confirm.disabled = true;
      var label = confirm.querySelector(".btn-label");
      if (label) label.textContent = "Updating…";
    }
    fetch("/app/api/update/apply", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify({ totp: code }),
    }).then(function(res) {
      return res.json().catch(function() { return {}; });
    }).then(function(data) {
      if (!box) return;
      if (data.ok) {
        var tail = data.restart_scheduled
          ? " The server restarts in " + data.restart_in_seconds +
            "s — this page will be briefly unreachable."
          : "";
        box.innerHTML = '<div class="ut-ok"><strong>Updated.</strong> ' +
          esc(data.message || "") + esc(tail) + '</div>';
        remember(state && state.latest_ref);
      } else {
        box.innerHTML = '<div class="ut-err"><strong>Update failed.</strong> ' +
          esc(data.message || data.error || "Unknown error.") + '</div>';
      }
    }).catch(function() {
      if (box) {
        box.innerHTML = '<div class="ut-err">Network error while updating.</div>';
      }
    });
  }

  fetch("/app/api/update", { credentials: "same-origin" })
    .then(function(res) { return res.ok ? res.json() : null; })
    .then(function(data) {
      if (!data || !data.enabled || !data.available) return;
      if (dismissed(data.latest_ref)) return;
      render(data);
    })
    .catch(function() {});
})();

// Two-step login: Client ID / Secret → TOTP.
// Submits all three fields together once the 6-digit code is entered.
(function() {
  var form = document.getElementById("login-form");
  if (!form) return;
  var s1 = document.getElementById("step-1");
  var s2 = document.getElementById("step-2");
  var toggle = document.getElementById("toggle-pw");
  var pw = document.getElementById("client_secret");
  var cid = document.getElementById("client_id");
  var toStep2 = document.getElementById("to-step-2");
  var back = document.getElementById("back-to-1");
  var totpHidden = document.getElementById("totp");
  var verifyBtn = document.getElementById("verify-btn");
  var inputs = document.querySelectorAll("#totp-inputs input");
  var verifiedLabel = document.getElementById("client-verified");

  if (toggle) {
    toggle.addEventListener("click", function() {
      pw.type = pw.type === "password" ? "text" : "password";
    });
  }

  function goStep2() {
    if (!cid.value.trim() || !pw.value) return;
    if (verifiedLabel) verifiedLabel.textContent = cid.value.trim();
    s1.hidden = true;
    s2.hidden = false;
    setTimeout(function() { if (inputs[0]) inputs[0].focus(); }, 40);
  }
  if (toStep2) toStep2.addEventListener("click", goStep2);

  // Allow Enter in step-1 inputs to advance rather than submit.
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

  function collectTotp() {
    var s = "";
    inputs.forEach(function(i) { s += (i.value || "").replace(/\D/g, ""); });
    return s;
  }
  function refresh() {
    var v = collectTotp();
    totpHidden.value = v;
    verifyBtn.disabled = v.length !== 6;
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

  form.addEventListener("submit", function(e) {
    if (totpHidden.value.length !== 6) {
      e.preventDefault();
      goStep2();
    }
  });
})();

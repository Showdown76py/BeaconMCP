// Shared 6-box TOTP input handler.
// Works on any form that contains #totp-inputs (inputs), #totp (hidden field),
// and #verify-btn (submit button). Used by /app/refresh and /oauth/authorize.
(function() {
  var container = document.getElementById("totp-inputs");
  if (!container) return;
  var form = container.closest("form");
  var totpHidden = document.getElementById("totp");
  var verifyBtn = document.getElementById("verify-btn");
  var inputs = container.querySelectorAll("input");
  if (!form || !totpHidden || !verifyBtn || !inputs.length) return;

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

  if (inputs[0] && !inputs[0].disabled) inputs[0].focus();
})();

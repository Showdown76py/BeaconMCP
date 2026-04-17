// Tokens page — copy-to-clipboard buttons.
(function() {
  var buttons = document.querySelectorAll("[data-copy]");
  buttons.forEach(function(btn) {
    btn.addEventListener("click", function() {
      var value = btn.getAttribute("data-copy") || "";
      var label = btn.querySelector("span");
      var original = label ? label.textContent : null;
      try {
        navigator.clipboard && navigator.clipboard.writeText(value);
        btn.classList.add("ok");
        if (label) label.textContent = "Copied";
        setTimeout(function() {
          btn.classList.remove("ok");
          if (label && original != null) label.textContent = original;
        }, 1600);
      } catch (err) {
        console.error(err);
      }
    });
  });
})();

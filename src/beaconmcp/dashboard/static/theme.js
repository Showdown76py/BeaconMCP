// Apply theme preference before first paint to avoid flash.
// Loaded synchronously from <head> so it blocks rendering until done.
(function() {
  try {
    var raw = localStorage.getItem("beaconmcp-ui-state");
    var s = raw ? JSON.parse(raw) : {};
    var t = s.theme || "auto";
    var dark = t === "dark" || (t === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  } catch (e) {}
})();

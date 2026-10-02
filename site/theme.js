(function () {
  var root = document.documentElement;
  var stored = null;
  try {
    stored = localStorage.getItem("kwork-mcp-theme");
  } catch (e) {}
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.className += " js";
  root.setAttribute("data-theme", stored === "light" || stored === "dark" ? stored : dark ? "dark" : "light");
})();

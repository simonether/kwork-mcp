(function () {
  "use strict";

  var root = document.documentElement;

  function $all(selector, scope) {
    return Array.prototype.slice.call((scope || document).querySelectorAll(selector));
  }

  /* ---------- Theme toggle ---------- */
  (function theme() {
    var button = document.querySelector("[data-theme-toggle]");
    var scheme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
    var hasStored = false;
    try {
      var saved = localStorage.getItem("kwork-mcp-theme");
      hasStored = saved === "light" || saved === "dark";
    } catch (e) {}

    if (button) {
      button.addEventListener("click", function () {
        var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        hasStored = true;
        try {
          localStorage.setItem("kwork-mcp-theme", next);
        } catch (e) {}
      });
    }
    if (scheme && scheme.addEventListener) {
      scheme.addEventListener("change", function (event) {
        if (!hasStored) root.setAttribute("data-theme", event.matches ? "dark" : "light");
      });
    }
  })();

  /* ---------- Copy buttons ---------- */
  (function copyButtons() {
    if (!navigator.clipboard || !window.isSecureContext) return;
    $all("[data-copy]").forEach(function (block) {
      var code = block.querySelector("code");
      if (!code) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "copy";
      button.textContent = "Копировать";
      button.addEventListener("click", function () {
        navigator.clipboard.writeText(code.textContent.replace(/\s+$/, "")).then(function () {
          button.textContent = "Скопировано";
          button.classList.add("is-done");
          setTimeout(function () {
            button.textContent = "Копировать";
            button.classList.remove("is-done");
          }, 1600);
        });
      });
      (block.querySelector("[data-copy-slot]") || block).appendChild(button);
    });
  })();

  /* ---------- user_id substitution ---------- */
  (function userId() {
    var input = document.getElementById("uid");
    if (!input) return;
    var placeholders = $all(".ph");
    input.addEventListener("input", function () {
      var digits = input.value.replace(/\D+/g, "").slice(0, 12);
      if (digits !== input.value) input.value = digits;
      placeholders.forEach(function (node) {
        node.textContent = digits || "123456";
        node.classList.toggle("is-set", digits !== "");
      });
    });
  })();

  /* ---------- Tabs (built from the panels, so no-JS shows them stacked) ---------- */
  (function tabs() {
    $all("[data-tabs]").forEach(function (group, index) {
      var panels = $all("[role=tabpanel]", group);
      if (panels.length < 2) return;
      var list = document.createElement("div");
      list.setAttribute("role", "tablist");
      list.setAttribute("aria-label", group.getAttribute("data-tabs") || "Варианты");
      var buttons = panels.map(function (panel, i) {
        var title = panel.querySelector(".tab-title");
        var tab = document.createElement("button");
        tab.type = "button";
        tab.setAttribute("role", "tab");
        tab.id = "tab-" + index + "-" + i;
        panel.setAttribute("aria-labelledby", tab.id);
        tab.setAttribute("aria-controls", panel.id);
        tab.textContent = title ? title.textContent : "Вариант " + (i + 1);
        list.appendChild(tab);
        return tab;
      });
      group.insertBefore(list, panels[0]);

      function select(i, focus) {
        buttons.forEach(function (tab, j) {
          var active = i === j;
          tab.setAttribute("aria-selected", active ? "true" : "false");
          tab.tabIndex = active ? 0 : -1;
          panels[j].hidden = !active;
        });
        if (focus) buttons[i].focus();
      }
      buttons.forEach(function (tab, i) {
        tab.addEventListener("click", function () {
          select(i, false);
        });
        tab.addEventListener("keydown", function (event) {
          var next = null;
          if (event.key === "ArrowRight") next = (i + 1) % buttons.length;
          else if (event.key === "ArrowLeft") next = (i - 1 + buttons.length) % buttons.length;
          else if (event.key === "Home") next = 0;
          else if (event.key === "End") next = buttons.length - 1;
          if (next !== null) {
            event.preventDefault();
            select(next, true);
          }
        });
      });
      select(0, false);
    });
  })();
})();

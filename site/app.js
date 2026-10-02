(function () {
  "use strict";

  var root = document.documentElement;
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function $all(selector, scope) {
    return Array.prototype.slice.call((scope || document).querySelectorAll(selector));
  }

  function wait(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
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

  /* ---------- Demo: one typed prompt, then a prepared write the visitor can confirm ---------- */
  (function demo() {
    var demoNode = document.querySelector("[data-demo]");
    if (!demoNode) return;

    var steps = $all("[data-step]", demoNode);
    var slip = demoNode.querySelector("[data-slip]");
    var chip = demoNode.querySelector("[data-chip]");
    var note = demoNode.querySelector("[data-note]");
    var promptLine = demoNode.querySelector("[data-type]");

    var texts = {
      pending: {
        chip: "Подготовлено",
        note: "На Kwork пока ничего не отправлено. Запрос уйдёт только после вашего подтверждения."
      },
      done: {
        chip: "Отправлено",
        note: "Вы подтвердили, сервер один раз отправил отклик на Kwork. Повторно он его не отправит."
      },
      cancelled: {
        chip: "Отменено",
        note: "Запись не подтверждена, на Kwork ничего не ушло."
      }
    };

    function setState(state) {
      slip.setAttribute("data-state", state);
      chip.textContent = texts[state].chip;
      note.textContent = texts[state].note;
    }

    var confirmButton = demoNode.querySelector("[data-confirm]");
    var cancelButton = demoNode.querySelector("[data-cancel]");
    var replayButton = demoNode.querySelector("[data-replay]");
    confirmButton.addEventListener("click", function () {
      setState("done");
      replayButton.focus();
    });
    cancelButton.addEventListener("click", function () {
      setState("cancelled");
      replayButton.focus();
    });
    replayButton.addEventListener("click", function () {
      setState("pending");
      confirmButton.focus();
    });

    function showAll() {
      steps.forEach(function (step) {
        step.classList.add("on");
      });
    }

    // Typed text: the real string stays in the DOM for assistive tech,
    // a visual copy is typed on top of it.
    var fullText = promptLine.textContent;
    var visual = document.createElement("span");
    visual.className = "typed";
    visual.setAttribute("aria-hidden", "true");
    promptLine.classList.add("sr-only");
    promptLine.parentNode.insertBefore(visual, promptLine);

    if (reduceMotion) {
      visual.textContent = fullText;
      showAll();
      return;
    }

    async function play() {
      steps[0].classList.add("on");
      visual.classList.add("caret");
      await wait(350);
      for (var i = 1; i <= fullText.length; i++) {
        visual.textContent = fullText.slice(0, i);
        await wait(fullText.charAt(i - 1) === " " ? 55 : 24);
      }
      visual.classList.remove("caret");
      for (var s = 1; s < steps.length; s++) {
        await wait(s === steps.length - 1 ? 600 : 380);
        steps[s].classList.add("on");
      }
    }

    function start() {
      var started = false;
      function once() {
        if (started) return;
        started = true;
        play();
      }
      if ("IntersectionObserver" in window) {
        var observer = new IntersectionObserver(
          function (entries) {
            if (entries.some(function (entry) { return entry.isIntersecting; })) {
              observer.disconnect();
              once();
            }
          },
          { threshold: 0.35 }
        );
        observer.observe(demoNode);
      } else {
        once();
      }
    }
    start();
  })();
})();

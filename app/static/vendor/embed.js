// External classic script (CSP script-src 'self'): renders each JSON spec block
// via the vendored global vegaEmbed, using the bundled AST interpreter
// (window.vegaInterp) so no new Function/eval is needed under a strict CSP.
// Idempotent + re-runnable after htmx swaps.
(function () {
  function renderAll(root) {
    (root || document)
      .querySelectorAll('script[type="application/json"][id$="-spec"]')
      .forEach(function (el) {
        if (el.dataset.rendered) return;
        el.dataset.rendered = "1";
        var target = document.getElementById(el.id.replace(/-spec$/, ""));
        if (!target) return;
        try {
          vegaEmbed(target, JSON.parse(el.textContent), {
            actions: false,
            ast: true,
            expr: window.vegaInterp.expressionInterpreter,
          });
        } catch (e) {
          target.textContent = "chart unavailable";
        }
      });
  }
  document.addEventListener("DOMContentLoaded", function () { renderAll(); });
  // Re-render charts inside htmx-swapped fragments (events bubble to document).
  document.addEventListener("htmx:afterSwap", function (e) {
    renderAll(e.target);
  });
})();

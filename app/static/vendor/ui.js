// Local UI behaviour (CSP script-src 'self'): client-side tab switching (all
// data loaded upfront) and checkbox-list filtering. No framework, no eval.
(function () {
  function activateTab(bar, idx) {
    var tabs = bar.parentElement;
    tabs.querySelectorAll(".tab-btn").forEach(function (b, i) {
      b.setAttribute("aria-selected", i === idx ? "true" : "false");
    });
    tabs.querySelectorAll(".tab-panel").forEach(function (p, i) {
      p.setAttribute("data-active", i === idx ? "1" : "0");
    });
    // Vega charts with width:"container" render at 0px while their panel is
    // display:none (nothing to measure) and don't recover on show; nudge them
    // to re-measure the now-visible container.
    window.dispatchEvent(new Event("resize"));
  }
  function wire(root) {
    (root || document).querySelectorAll(".tab-bar").forEach(function (bar) {
      if (bar.dataset.wired) return;
      bar.dataset.wired = "1";
      bar.querySelectorAll(".tab-btn").forEach(function (btn, i) {
        btn.addEventListener("click", function () { activateTab(bar, i); });
      });
    });
    (root || document).querySelectorAll("[data-cbfilter]").forEach(function (inp) {
      if (inp.dataset.wired) return;
      inp.dataset.wired = "1";
      inp.addEventListener("input", function () {
        var q = inp.value.toLowerCase();
        inp.parentElement.querySelectorAll(".cb").forEach(function (cb) {
          cb.style.display = cb.textContent.toLowerCase().indexOf(q) >= 0 ? "" : "none";
        });
      });
    });
  }
  document.addEventListener("DOMContentLoaded", function () { wire(); });
  document.addEventListener("htmx:afterSwap", function (e) { wire(e.target); });
})();

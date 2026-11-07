// Agent Fire Drill: progressive enhancement for the live drill page only. Polls the request's real status (the same data
// the page renders server-side) and updates the stage list and elapsed time. Without JavaScript the page refreshes itself.
(function () {
  var el = document.querySelector("[data-request]");
  if (!el) return;
  var id = el.getAttribute("data-request");
  var started = parseFloat(el.getAttribute("data-accepted") || "0");
  var elapsed = document.querySelector("[data-elapsed]");
  var stateText = document.querySelector("[data-state-text]");
  var stages = document.querySelectorAll("[data-stage]");
  function fmt(s) { s = Math.max(0, Math.floor(s)); return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2); }
  function tick() { if (started && elapsed) elapsed.textContent = fmt(Date.now() / 1000 - started); }
  function apply(d) {
    if (d.accepted_at) started = d.accepted_at;
    stages.forEach(function (li) {
      var n = parseInt(li.getAttribute("data-stage"), 10);
      li.className = n < d.stage ? "done" : (n === d.stage ? (d.failed ? "failed" : "now") : "");
    });
    if (stateText) stateText.textContent = d.detail || "";
    if (d.state === "done" && d.bundle) { window.location.href = "/results/" + encodeURIComponent(d.bundle) + "?live=" + encodeURIComponent(id); return true; }
    return d.state === "failed" || d.state === "rejected";
  }
  function poll() {
    fetch("/api/requests/" + encodeURIComponent(id) + ".json", { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (!d || !apply(d)) setTimeout(poll, 3000); })
      .catch(function () { setTimeout(poll, 5000); });
  }
  document.documentElement.classList.add("js");
  setInterval(tick, 1000); tick(); poll();
})();

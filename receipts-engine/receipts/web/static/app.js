// Receipts console: copy buttons, manual-check upload, seed-test logging. No dependencies.
(function () {
  function toast(el, msg, ok) {
    el.textContent = msg;
    el.className = "toast badge " + (ok ? "b-pass" : "b-fail");
  }

  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-copy]");
    if (!b) return;
    var src = document.getElementById(b.getAttribute("data-copy"));
    if (!src) return;
    navigator.clipboard.writeText(src.innerText).then(function () {
      var old = b.textContent; b.textContent = "Copied"; setTimeout(function () { b.textContent = old; }, 1200);
    });
  });

  document.querySelectorAll("form.manual-check").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      var out = f.querySelector(".toast"), btn = f.querySelector("button"), file = f.screenshot.files[0];
      if (!file) { toast(out, "Attach a screenshot of the profile showing recent activity.", false); return; }
      var reader = new FileReader();
      btn.disabled = true;
      reader.onload = function () {
        var b64 = String(reader.result).split(",")[1] || "";
        fetch("/api/manual-check", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({lead_id: f.dataset.lead, channel: f.dataset.channel, active_date: f.active_date.value,
                                note: f.note.value, filename: file.name, screenshot_b64: b64})
        }).then(function (r) { return r.json(); }).then(function (j) {
          btn.disabled = false;
          if (!j.ok) { toast(out, j.error, false); return; }
          toast(out, j.verified ? "Verified. Best: " + j.best + ", backup: " + j.backup + ". Reloading…"
                                : "Recorded but not verified (name match or date too old).", j.verified);
          if (j.verified) setTimeout(function () { location.reload(); }, 900);
        }).catch(function () { btn.disabled = false; toast(out, "Request failed.", false); });
      };
      reader.readAsDataURL(file);
    });
  });

  document.querySelectorAll("form.seed-log").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      var out = f.querySelector(".toast");
      var data = {action: f.dataset.action, lead_id: f.lead_id.value, channel: f.channel.value};
      if (f.label) data.label = f.label.value;
      if (f.step) data.step = f.step.value;
      fetch("/api/seed/log", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)})
        .then(function (r) { return r.json(); })
        .then(function (j) { toast(out, j.ok ? "Logged. Reloading…" : j.error, j.ok); if (j.ok) setTimeout(function () { location.reload(); }, 700); });
    });
  });

  var hit = document.getElementById("hit");
  if (hit) hit.scrollIntoView({block: "center"});
})();

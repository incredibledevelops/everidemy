/* =====================================================
   Everidemy — Attendance mark sheet
   ===================================================== */

(function () {
  "use strict";

  const form = document.getElementById("attendance-form");
  if (!form) return;

  const statusInputs = form.querySelectorAll("[data-status-input]");
  const rows = form.querySelectorAll("[data-student-row]");

  // ---------- Summary counters ----------
  function updateSummary() {
    const counts = { present: 0, absent: 0, late: 0, excused: 0 };

    rows.forEach((row) => {
      const checked = row.querySelector("[data-status-input]:checked");
      if (checked && counts[checked.value] !== undefined) {
        counts[checked.value] += 1;
      }
    });

    const ids = ["present", "absent", "late", "excused"];
    ids.forEach((key) => {
      const el = document.getElementById("sum-" + key);
      if (el) el.textContent = counts[key];
    });
  }

  // ---------- Batch actions ----------
  window.markAll = function (status) {
    rows.forEach((row) => {
      const input = row.querySelector(`[data-status-input][value="${status}"]`);
      if (input) input.checked = true;
    });
    updateSummary();
  };

  window.clearAll = function () {
    rows.forEach((row) => {
      row.querySelectorAll("[data-status-input]").forEach((i) => (i.checked = false));
    });
    updateSummary();
  };

  // ---------- Bind change events ----------
  statusInputs.forEach((input) => {
    input.addEventListener("change", updateSummary);
  });

  // ---------- Initial summary ----------
  updateSummary();

  // ---------- Warn on unload if unsaved ----------
  let dirty = false;
  form.addEventListener("change", () => { dirty = true; });
  form.addEventListener("submit", () => { dirty = false; });

  window.addEventListener("beforeunload", (e) => {
    if (dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  // ---------- Keyboard shortcuts ----------
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      form.submit();
    }
  });
})();
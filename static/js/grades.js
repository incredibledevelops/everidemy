/* =====================================================
   Everidemy — Grade entry live calculator
   ===================================================== */

(function () {
  "use strict";

  const form = document.getElementById("grades-form");
  if (!form) return;

  const CA_MAX = 30;
  const EXAM_MAX = 70;

  const GRADE_SCALE = [
    [80, "A1", "Excellent", "bg-green-50 text-green-700"],
    [75, "B2", "Very Good", "bg-green-50 text-green-700"],
    [70, "B3", "Good",      "bg-green-50 text-green-700"],
    [65, "C4", "Credit",    "bg-blue-50 text-blue-700"],
    [60, "C5", "Credit",    "bg-blue-50 text-blue-700"],
    [55, "C6", "Credit",    "bg-blue-50 text-blue-700"],
    [50, "D7", "Pass",      "bg-amber-50 text-amber-700"],
    [45, "E8", "Weak Pass", "bg-amber-50 text-amber-700"],
    [0,  "F9", "Fail",      "bg-red-50 text-red-700"],
  ];

  function gradeFor(total) {
    for (const [threshold, letter, remark, cls] of GRADE_SCALE) {
      if (total >= threshold) return { letter, remark, cls };
    }
    return { letter: "F9", remark: "Fail", cls: "bg-red-50 text-red-700" };
  }

  function clamp(v, max) {
    const n = parseInt(v, 10);
    if (isNaN(n)) return 0;
    return Math.max(0, Math.min(n, max));
  }

  const rows = form.querySelectorAll("[data-student-row]");

  function recompute(row) {
    const caInput   = row.querySelector("[data-ca-input]");
    const examInput = row.querySelector("[data-exam-input]");
    const totalEl   = row.querySelector("[data-total]");
    const gradeEl   = row.querySelector("[data-grade]");
    const remarkEl  = row.querySelector("[data-remark]");

    const caRaw   = caInput.value.trim();
    const examRaw = examInput.value.trim();

    // If both are blank, show "—"
    if (caRaw === "" && examRaw === "") {
      totalEl.textContent  = "—";
      gradeEl.textContent  = "—";
      gradeEl.className    = "inline-block text-xs font-bold px-2 py-1 rounded bg-slate-100 text-slate-500";
      remarkEl.textContent = "—";
      return;
    }

    const ca = clamp(caRaw, CA_MAX);
    const exam = clamp(examRaw, EXAM_MAX);
    const total = ca + exam;
    const { letter, remark, cls } = gradeFor(total);

    totalEl.textContent = total;
    gradeEl.textContent = letter;
    gradeEl.className = "inline-block text-xs font-bold px-2 py-1 rounded " + cls;
    remarkEl.textContent = remark;
  }

  rows.forEach((row) => {
    const caInput   = row.querySelector("[data-ca-input]");
    const examInput = row.querySelector("[data-exam-input]");
    caInput.addEventListener("input", () => recompute(row));
    examInput.addEventListener("input", () => recompute(row));
  });

  // Ctrl/Cmd + Enter to save
  form.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      form.submit();
    }
  });

  // Warn on unsaved changes
  let dirty = false;
  form.addEventListener("input", () => { dirty = true; });
  form.addEventListener("submit", () => { dirty = false; });
  window.addEventListener("beforeunload", (e) => {
    if (dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
})();
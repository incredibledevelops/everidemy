/* =====================================================
   Everidemy — Timetable builder
   ===================================================== */

(function () {
  "use strict";

  const modal = document.getElementById("slot-modal");
  if (!modal) return;

  window.openSlotModal = function (day) {
    // Pre-select the day (if provided)
    if (day) {
      const select = modal.querySelector('select[name="day"]');
      if (select) {
        select.value = day;
      }
    }
    modal.classList.remove("hidden");
    document.body.style.overflow = "hidden";

    // Focus first field
    const first = modal.querySelector("input, select, textarea");
    if (first) setTimeout(() => first.focus(), 50);
  };

  window.closeSlotModal = function () {
    modal.classList.add("hidden");
    document.body.style.overflow = "";
  };

  // Escape to close
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) {
      window.closeSlotModal();
    }
  });

  // Auto-calculate end time when start changes (+45 min)
  const startInput = modal.querySelector('input[name="start"]');
  const endInput = modal.querySelector('input[name="end"]');

  if (startInput && endInput) {
    startInput.addEventListener("change", () => {
      const [h, m] = startInput.value.split(":").map(Number);
      if (isNaN(h) || isNaN(m)) return;
      const endDate = new Date();
      endDate.setHours(h, m + 45, 0, 0);
      const hh = String(endDate.getHours()).padStart(2, "0");
      const mm = String(endDate.getMinutes()).padStart(2, "0");
      endInput.value = `${hh}:${mm}`;
    });
  }
})();
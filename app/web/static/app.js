/**
 * Feed interactions.
 *
 * The status buttons post through ``fetch()`` so a dismissed row animates out on
 * its own instead of reloading the whole page. The endpoint answers with JSON only
 * when the caller asks for it (``Accept: application/json``), so the vacancy card
 * and no-JS browsers keep the plain 303 redirect.
 */
(() => {
  const SWIPE_MS = 320;

  const feed = document.querySelector(".feed");
  const swipeMode = feed?.dataset.swipe === "true";

  // Stagger the initial reveal.
  document.querySelectorAll(".row, .detail").forEach((el, i) => {
    el.style.animationDelay = `${Math.min(i * 0.03, 0.35)}s`;
  });

  /** Funnel counters are re-read from the server response, never guessed. */
  function syncFunnel(funnel) {
    if (!funnel) return;
    document.querySelectorAll(".funnel-cell[data-status]").forEach((cell) => {
      const n = cell.querySelector(".funnel-n");
      if (n) n.textContent = funnel[cell.dataset.status] ?? 0;
    });
  }

  /** Collapse the row (height → 0) so the list closes the gap smoothly. */
  function collapse(row) {
    row.style.overflow = "hidden";
    row.style.transition = `opacity ${SWIPE_MS}ms ease, transform ${SWIPE_MS}ms ease, ` +
      `height ${SWIPE_MS}ms ease, margin ${SWIPE_MS}ms ease, padding ${SWIPE_MS}ms ease, ` +
      `border-width ${SWIPE_MS}ms ease`;
    row.style.height = `${row.offsetHeight}px`;
    // Force a reflow so the browser has a start value to animate from.
    void row.offsetHeight;
    row.style.opacity = "0";
    row.style.transform = "translateX(-24px)";
    row.style.height = "0px";
    row.style.marginTop = "0px";
    row.style.marginBottom = "0px";
    row.style.paddingTop = "0px";
    row.style.paddingBottom = "0px";
    row.style.borderWidth = "0px";
    window.setTimeout(() => row.remove(), SWIPE_MS);
  }

  function updateMeta(removed) {
    const meta = document.querySelector(".meta");
    if (!meta || !removed) return;
    meta.textContent = meta.textContent.replace(/^\d+/, (n) => String(Math.max(0, Number(n) - 1)));
  }

  /** Send one status change and animate the row out. */
  async function decide(row, status, reason) {
    if (row.dataset.busy === "1") return;
    row.dataset.busy = "1";
    try {
      const body = new URLSearchParams({ status, reason: reason ?? "", next: "/" });
      const resp = await fetch(`/jobs/${row.dataset.id}/status`, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
      if (!resp.ok) throw new Error(`status ${resp.status}`);
      const data = await resp.json();
      syncFunnel(data.funnel);
      collapse(row);
      updateMeta(true);
    } catch (err) {
      row.dataset.busy = "";
      // Fall back to a normal reload so the state on screen is never a lie.
      window.location.reload();
    }
  }

  if (feed) {
    feed.addEventListener("submit", (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || !form.closest(".row-actions")) return;
      event.preventDefault();
      const row = form.closest(".row");
      if (!row) return;
      const field = form.querySelector('input[name="status"]');
      const reason = form.querySelector('input[name="reason"]');
      void decide(row, field ? field.value : "", reason ? reason.value : "");
    });
  }

  /** Keyboard shortcuts for the new-vacancy pool: S — save, X — dismiss. */
  if (swipeMode) {
    document.addEventListener("keydown", (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const tag = document.activeElement?.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      const key = event.key.toLowerCase();
      if (key !== "s" && key !== "x") return;
      const row = document.querySelector(".row:not([data-busy='1'])");
      if (!row) return;
      event.preventDefault();
      void decide(row, key === "s" ? "saved" : "ignored", key === "s" ? "" : "other");
    });
  }
})();

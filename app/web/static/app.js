(() => {
  const rows = document.querySelectorAll(".row, .detail");
  rows.forEach((el, i) => {
    el.style.animationDelay = `${Math.min(i * 0.03, 0.35)}s`;
  });
})();

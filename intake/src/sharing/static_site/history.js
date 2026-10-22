(() => {
  "use strict";

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const rows = document.querySelectorAll(".reveal");

  if ("IntersectionObserver" in window && !reducedMotion) {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("visible");
          observer.unobserve(entry.target);
        });
      },
      { threshold: 0.06 },
    );
    rows.forEach((row) => observer.observe(row));
  } else {
    rows.forEach((row) => row.classList.add("visible"));
  }

  window.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const destination = event.key === "ArrowLeft"
      ? document.body.dataset.prev
      : event.key === "ArrowRight"
        ? document.body.dataset.next
        : "";
    if (destination) window.location.assign(destination);
  });
})();

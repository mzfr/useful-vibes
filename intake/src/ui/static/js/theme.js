// Set the dark class before first paint to avoid FOUC.
// Alpine takes over once it loads; this just prevents the flash.
(function () {
  try {
    if (localStorage.getItem("intake-theme") === "dark") {
      document.documentElement.classList.add("dark");
    }
  } catch (_) {}
})();

(() => {
  let stored = null;
  try {
    stored = localStorage.getItem("gameforge.theme");
  } catch {
    // Storage can be unavailable; the system preference remains authoritative.
  }
  const theme =
    stored === "light" || stored === "dark"
      ? stored
      : matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
})();

try {
  var p = localStorage.getItem("avelis.theme");
  document.documentElement.dataset.theme =
    p === "dark" || p === "light"
      ? p
      : p === "system"
        ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
        : "light";
} catch (e) {}

(() => {
  const el = document.getElementById("countdown");
  if (!el || !el.dataset.iso) return;
  const target = new Date(el.dataset.iso);
  const tick = () => {
    const ms = target.getTime() - Date.now();
    if (ms <= 0) {
      el.textContent = "en cours / voir demain 18:00 TU";
      return;
    }
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    el.textContent = (d ? `${d} j ` : "") + `${String(h).padStart(2, "0")} h ${String(m).padStart(2, "0")} min`;
  };
  tick();
  setInterval(tick, 30000);
})();

(() => {
  const el = document.getElementById("countdown");
  if (el && el.dataset.iso) {
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
  }

  const TOKEN_KEY = "ggr-admin-token";
  document.querySelectorAll("[data-delete-vacation]").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = btn.getAttribute("data-delete-vacation");
      if (!id) return;
      if (!window.confirm(`Supprimer définitivement ${id} (audio, vidéo, dossier) ?`)) return;
      const token = localStorage.getItem(TOKEN_KEY) || window.prompt("Jeton administrateur :") || "";
      if (token) localStorage.setItem(TOKEN_KEY, token);
      if (!token) {
        window.alert("Jeton manquant. Ouvre Réglages et colle le jeton, puis réessaie.");
        return;
      }
      btn.disabled = true;
      try {
        const res = await fetch("/api/vacations/" + encodeURIComponent(id), {
          method: "DELETE",
          headers: { "X-Admin-Token": token },
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
          if (window.location.pathname.indexOf("/vacations/") === 0) {
            window.location.href = "/";
          } else {
            const card = btn.closest(".card");
            if (card) card.remove();
          }
          return;
        }
        const detail = Array.isArray(data.detail)
          ? data.detail.map((x) => x.msg || x).join(" ")
          : data.detail;
        window.alert(detail || `Erreur ${res.status}`);
      } catch (err) {
        window.alert(String(err));
      } finally {
        btn.disabled = false;
      }
    });
  });
})();

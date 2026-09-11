(() => {
  const TOKEN_KEY = "ggr-admin-token";
  const token = () => localStorage.getItem(TOKEN_KEY) || "";

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

  const hasToken = !!token();
  document.querySelectorAll(".js-del-vac").forEach((btn) => {
    btn.hidden = !hasToken;
  });
  document.querySelectorAll(".js-del-need-token").forEach((n) => {
    n.hidden = hasToken;
  });

  document.querySelectorAll(".js-del-vac").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = btn.getAttribute("data-delete-vacation");
      const tok = token();
      if (!id) return;
      if (!tok) {
        window.alert("Suppression refusée : renseigne le jeton dans Réglages, puis recharge.");
        return;
      }
      if (!window.confirm(`Supprimer définitivement ${id} (audio, vidéo, dossier) ?`)) return;
      btn.disabled = true;
      try {
        const res = await fetch("/api/vacations/" + encodeURIComponent(id) + "/delete", {
          method: "POST",
          headers: { "X-Admin-Token": tok },
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
        window.alert(detail || `Suppression impossible (${res.status})`);
      } catch (err) {
        window.alert(String(err));
      } finally {
        btn.disabled = false;
      }
    });
  });
})();

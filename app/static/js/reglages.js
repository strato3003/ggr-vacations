(() => {
  const form = document.getElementById("reglages-form");
  const banner = document.getElementById("settings-banner");
  const recordBtn = document.getElementById("record-now");
  const recordQrgBtn = document.getElementById("record-qrg");
  if (!form) return;

  const TOKEN_KEY = "ggr-admin-token";
  const tokenInput = form.elements.namedItem("admin_token");
  if (tokenInput && !tokenInput.value) {
    tokenInput.value = localStorage.getItem(TOKEN_KEY) || "";
  }

  const slot = (name) => form.querySelector(`.settings-msg[data-for="${name}"]`);

  const say = (name, text, ok) => {
    const el = slot(name);
    if (el) {
      el.hidden = false;
      el.textContent = text;
      el.classList.toggle("err", !ok);
      el.classList.toggle("ok", !!ok);
      el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    if (banner) {
      banner.hidden = false;
      banner.textContent = text;
      banner.classList.toggle("err", !ok);
      banner.classList.toggle("ok", !!ok);
    }
  };

  const busy = (btn, on) => {
    if (!btn) return;
    btn.disabled = on;
  };

  const payload = () => ({
    tx_khz: Number(form.elements.namedItem("tx_khz").value),
    ack1_khz: Number(form.elements.namedItem("ack1_khz").value),
    ack2_khz: Number(form.elements.namedItem("ack2_khz").value),
    qrg_tolerance_khz: Number(form.elements.namedItem("qrg_tolerance_khz").value),
    lead_minutes: Number(form.elements.namedItem("lead_minutes").value),
    duration_minutes: Number(form.elements.namedItem("duration_minutes").value),
  });

  const headers = () => {
    const token = (tokenInput && tokenInput.value) || "";
    if (token) localStorage.setItem(TOKEN_KEY, token);
    return {
      "Content-Type": "application/json",
      "X-Admin-Token": token,
    };
  };

  const _detail = (data) => {
    const d = data && data.detail;
    if (Array.isArray(d)) {
      return d.map((x) => x.msg || JSON.stringify(x)).join(" ");
    }
    return d;
  };

  const saveSettings = async () => {
    const res = await fetch("/api/settings", {
      method: "PUT",
      headers: headers(),
      body: JSON.stringify(payload()),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(_detail(data) || `Erreur ${res.status}`);
    }
    return data;
  };

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = document.getElementById("save-qrg");
    busy(btn, true);
    say("save", "Sauvegarde en cours…", true);
    try {
      const data = await saveSettings();
      say(
        "save",
        `Fréquences mémorisées (pas d’enregistrement radio) : ${data.tx_mhz} / ${data.ack1_mhz} / ${data.ack2_mhz} MHz · avance ${data.schedule_lead} min · durée ${data.duration_minutes} min.`,
        true
      );
    } catch (err) {
      say("save", String(err), false);
    } finally {
      busy(btn, false);
    }
  });

  if (recordQrgBtn) {
    recordQrgBtn.addEventListener("click", async () => {
      busy(recordQrgBtn, true);
      say("qrg", "Contact du serveur, démarrage du test…", true);
      const huntEl = form.elements.namedItem("test_hunt");
      try {
        const res = await fetch("/api/vacations/record", {
          method: "POST",
          headers: headers(),
          body: JSON.stringify({
            freq_khz: Number(form.elements.namedItem("test_freq").value),
            duration_minutes: Number(form.elements.namedItem("test_duration_minutes").value),
            hunt: !!(huntEl && huntEl.checked),
            qrg_tolerance_khz: Number(form.elements.namedItem("qrg_tolerance_khz").value),
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 202) {
          say(
            "qrg",
            `Test radio lancé sur ${data.freq_mhz} MHz pendant ${data.duration_minutes} min` +
              `${data.hunt ? " (chasse USB ± QRM)" : ""}. Une carte « Record … MHz » apparaîtra sur l’accueil à la fin.`,
            true
          );
          return;
        }
        say("qrg", _detail(data) || `Erreur ${res.status}`, false);
      } catch (err) {
        say("qrg", String(err), false);
      } finally {
        busy(recordQrgBtn, false);
      }
    });
  }

  if (recordBtn) {
    recordBtn.addEventListener("click", async () => {
      busy(recordBtn, true);
      say("vac", "Sauvegarde des QRG puis démarrage de la vacation…", true);
      try {
        await saveSettings();
        const res = await fetch("/api/vacations/record", {
          method: "POST",
          headers: headers(),
          body: JSON.stringify({
            duration_minutes: Number(form.elements.namedItem("duration_minutes").value),
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 202) {
          say(
            "vac",
            `Vacation complète lancée (${data.duration_minutes} min) : bulletin + ACK flotte / France / Tahiti. Carte sur l’accueil à la fin.`,
            true
          );
          return;
        }
        say("vac", _detail(data) || `Erreur ${res.status}`, false);
      } catch (err) {
        say("vac", String(err), false);
      } finally {
        busy(recordBtn, false);
      }
    });
  }
})();

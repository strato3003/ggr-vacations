(() => {
  const form = document.getElementById("reglages-form");
  const msg = document.getElementById("settings-msg");
  const recordBtn = document.getElementById("record-now");
  const recordQrgBtn = document.getElementById("record-qrg");
  if (!form) return;

  const TOKEN_KEY = "ggr-admin-token";
  const tokenInput = form.elements.namedItem("admin_token");
  if (tokenInput && !tokenInput.value) {
    tokenInput.value = localStorage.getItem(TOKEN_KEY) || "";
  }

  const say = (text, ok) => {
    if (!msg) return;
    msg.textContent = text;
    msg.classList.toggle("err", !ok);
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
    say("Enregistrement…", true);
    try {
      const data = await saveSettings();
      say(
        `QRG enregistrées : ${data.tx_mhz} / ${data.ack1_mhz} / ${data.ack2_mhz} MHz · ` +
          `avance ${data.schedule_lead} min · durée ${data.duration_minutes} min.`,
        true
      );
    } catch (err) {
      say(String(err), false);
    }
  });

  if (recordQrgBtn) {
    recordQrgBtn.addEventListener("click", async () => {
      say("Démarrage du record…", true);
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
            `Record lancé sur ${data.freq_mhz} MHz (${data.duration_minutes} min` +
              `${data.hunt ? ", suivi USB" : ""}) — carte sur l’accueil à la fin.`,
            true
          );
          return;
        }
        say(_detail(data) || `Erreur ${res.status}`, false);
      } catch (err) {
        say(String(err), false);
      }
    });
  }

  if (recordBtn) {
    recordBtn.addEventListener("click", async () => {
      say("Sauvegarde puis vacation complète…", true);
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
            `Vacation lancée (${data.duration_minutes} min, bulletin + ACK flotte / France / Tahiti). ` +
              "La carte apparaîtra sur l’accueil à la fin.",
            true
          );
          return;
        }
        say(_detail(data) || `Erreur ${res.status}`, false);
      } catch (err) {
        say(String(err), false);
      }
    });
  }
})();

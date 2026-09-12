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

  const payload = () => {
    const skipperBoxes = [...form.querySelectorAll('input[name="buddy_skipper"]:checked')];
    const skipperText = form.elements.namedItem("buddy_skippers_text");
    const skippers = skipperBoxes.length
      ? skipperBoxes.map((el) => el.value)
      : skipperText
        ? String(skipperText.value || "")
            .split("\n")
            .map((s) => s.trim())
            .filter(Boolean)
        : [];
    const enabledEl = form.elements.namedItem("buddy_enabled");
    const fleetEl = form.elements.namedItem("buddy_include_fleet");
    return {
      tx_khz: Number(form.elements.namedItem("tx_khz").value),
      ack1_khz: Number(form.elements.namedItem("ack1_khz").value),
      ack2_khz: Number(form.elements.namedItem("ack2_khz").value),
      qrg_tolerance_khz: Number(form.elements.namedItem("qrg_tolerance_khz").value),
      lead_minutes: Number(form.elements.namedItem("lead_minutes").value),
      duration_minutes: Number(form.elements.namedItem("duration_minutes").value),
      buddy_enabled: !!(enabledEl && enabledEl.checked),
      buddy_main_khz: Number(form.elements.namedItem("buddy_main_khz").value),
      buddy_alt_khz: Number(form.elements.namedItem("buddy_alt_khz").value),
      buddy_time_utc: form.elements.namedItem("buddy_time_utc").value,
      buddy_lead: Number(form.elements.namedItem("buddy_lead").value),
      buddy_duration_minutes: Number(form.elements.namedItem("buddy_duration_minutes").value),
      buddy_kiwi_count: Number(form.elements.namedItem("buddy_kiwi_count").value),
      buddy_include_fleet: !!(fleetEl && fleetEl.checked),
      buddy_skippers: skippers,
    };
  };

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
        `Fréquences mémorisées : ${data.tx_mhz} / ${data.ack1_mhz} / ${data.ack2_mhz} MHz · buddy ${data.buddy_main_khz} / ${data.buddy_alt_khz} kHz à ${data.buddy_time_utc} TU.`,
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

  const saveBuddyBtn = document.getElementById("save-buddy");
  if (saveBuddyBtn) {
    saveBuddyBtn.addEventListener("click", async () => {
      busy(saveBuddyBtn, true);
      say("buddy", "Sauvegarde du buddy call…", true);
      try {
        const data = await saveSettings();
        say(
          "buddy",
          `Buddy call mémorisé : ${data.buddy_main_khz} / ${data.buddy_alt_khz} kHz à ${data.buddy_time_utc} TU` +
            ` · ${data.buddy_enabled ? "actif" : "désactivé"}` +
            ` · ${data.buddy_include_fleet ? "centroïde flotte" : (data.buddy_skippers || []).join(", ") || "aucun skipper"}` +
            ` · ${data.buddy_kiwi_count} Kiwi.`,
          true
        );
      } catch (err) {
        say("buddy", String(err), false);
      } finally {
        busy(saveBuddyBtn, false);
      }
    });
  }

  const recordBuddyBtn = document.getElementById("record-buddy");
  if (recordBuddyBtn) {
    recordBuddyBtn.addEventListener("click", async () => {
      busy(recordBuddyBtn, true);
      say("buddy", "Sauvegarde puis démarrage du buddy call…", true);
      try {
        await saveSettings();
        const res = await fetch("/api/vacations/record", {
          method: "POST",
          headers: headers(),
          body: JSON.stringify({
            kind: "buddy",
            duration_minutes: Number(form.elements.namedItem("buddy_duration_minutes").value),
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 202) {
          say(
            "buddy",
            `Buddy call lancé (${data.duration_minutes} min) : 4483 et 6516 kHz sur plusieurs Kiwi. Carte sur l’accueil à la fin.`,
            true
          );
          return;
        }
        say("buddy", _detail(data) || `Erreur ${res.status}`, false);
      } catch (err) {
        say("buddy", String(err), false);
      } finally {
        busy(recordBuddyBtn, false);
      }
    });
  }
})();

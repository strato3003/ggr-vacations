(() => {
  const el = document.getElementById("ggr-map");
  const blob = document.getElementById("ggr-map-data");
  if (!el || !blob || typeof L === "undefined") return;

  let data;
  try {
    data = JSON.parse(blob.textContent || "{}");
  } catch {
    return;
  }

  const boats = (data.boats || []).filter((b) => Number.isFinite(b.lat) && Number.isFinite(b.lon));
  const kiwis = (data.kiwis || []).filter((k) => Number.isFinite(k.lat) && Number.isFinite(k.lon));
  const cent = data.centroid || {};

  const map = L.map(el, { scrollWheelZoom: true, worldCopyJump: true });

  // Carto « dark_all » exige désormais une clé (filigrane API KEY REQUIRED).
  // Fond satellite comme le tracker YB (GOOGLE_SATELLITE), sans clé Google.
  const satellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    {
      attribution:
        "Tuiles © Esri, Maxar, Earthstar Geographics · positions " +
        '<a href="https://yb.tl/ggr2026">Yellowbrick</a>',
      maxZoom: 18,
    }
  );
  const osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · positions ' +
      '<a href="https://yb.tl/ggr2026">Yellowbrick</a>',
    maxZoom: 19,
  });
  satellite.addTo(map);

  const boatLayer = L.layerGroup();
  const sdrLayer = L.layerGroup();
  const bounds = [];

  boats.forEach((b) => {
    const ll = [b.lat, b.lon];
    bounds.push(ll);
    const colour = /^#[0-9a-fA-F]{3,8}$/.test(b.colour || "") ? b.colour : "#c9a227";
    const track = (b.track || []).filter((p) => Array.isArray(p) && p.length === 2);
    if (track.length > 1) {
      L.polyline(track, { color: colour, weight: 2, opacity: 0.85 }).addTo(boatLayer);
    }
    const title = b.name || "Bateau";
    L.marker(ll, { icon: boatIcon(colour, b.heading), zIndexOffset: 400 })
      .bindTooltip(
        `<span style="border-left:3px solid ${esc(colour)};padding-left:5px">${esc(title)}</span>`,
        {
          permanent: true,
          direction: "right",
          offset: [14, 0],
          className: "ggr-yb-label",
          opacity: 1,
        }
      )
      .bindPopup(boatPopup(b, title))
      .addTo(boatLayer);
  });

  if (Number.isFinite(cent.lat) && Number.isFinite(cent.lon)) {
    const ll = [cent.lat, cent.lon];
    bounds.push(ll);
    L.circleMarker(ll, {
      radius: 10,
      color: "#f4e6c3",
      weight: 2,
      fillColor: "#081018",
      fillOpacity: 0.25,
    })
      .bindTooltip("Centroïde", {
        permanent: false,
        direction: "top",
        className: "ggr-yb-label",
      })
      .bindPopup(
        `<strong>${esc(cent.label || "Centroïde flotte")}</strong><br>` +
          `${esc(cent.fmt || fmt(cent.lat) + ", " + fmt(cent.lon))}`
      )
      .addTo(boatLayer);
  }

  kiwis.forEach((k) => {
    const ll = [k.lat, k.lon];
    bounds.push(ll);
    const label = sdrLabel(k);
    L.marker(ll, { icon: sdrIcon(), zIndexOffset: 300 })
      .bindTooltip(esc(label), {
        permanent: true,
        direction: "right",
        offset: [12, 0],
        className: "ggr-sdr-label",
        opacity: 1,
      })
      .bindPopup(sdrPopup(k))
      .addTo(sdrLayer);
  });

  boatLayer.addTo(map);
  sdrLayer.addTo(map);
  L.control
    .layers(
      { Satellite: satellite, OpenStreetMap: osm },
      { "Bateaux GGR": boatLayer, KiwiSDR: sdrLayer },
      { collapsed: false }
    )
    .addTo(map);

  if (bounds.length === 1) {
    map.setView(bounds[0], 6);
  } else if (bounds.length) {
    map.fitBounds(bounds, { padding: [36, 36], maxZoom: 9 });
  } else {
    map.setView([46.5, -1.79], 4);
  }

  function boatIcon(colour, heading) {
    const rot = Number.isFinite(heading) ? heading : 0;
    const html =
      `<div class="ggr-boat-mark" style="transform:rotate(${rot}deg)">` +
      `<svg viewBox="0 0 24 36" width="18" height="27" aria-hidden="true">` +
      `<path d="M12 1.5 L22.5 34 L12 26.5 L1.5 34 Z" fill="${esc(colour)}" stroke="#111" stroke-width="1.4"/>` +
      `</svg></div>`;
    return L.divIcon({
      className: "ggr-boat-icon",
      html,
      iconSize: [18, 27],
      iconAnchor: [9, 16],
    });
  }

  function sdrIcon() {
    const html =
      `<div class="ggr-sdr-mark">` +
      `<svg viewBox="0 0 22 22" width="18" height="18" aria-hidden="true">` +
      `<rect x="1.5" y="1.5" width="19" height="19" rx="3" fill="#3dba7a" stroke="#0b2a18" stroke-width="1.4"/>` +
      `<path d="M11 5 v8 M7.5 9.5 h7" stroke="#081018" stroke-width="1.8" fill="none"/>` +
      `</svg></div>`;
    return L.divIcon({
      className: "ggr-sdr-icon",
      html,
      iconSize: [18, 18],
      iconAnchor: [9, 9],
    });
  }

  function boatPopup(b, title) {
    const cap = Number.isFinite(b.heading) ? `${Math.round(b.heading)}°` : "—";
    return (
      `<strong>${esc(title)}</strong>` +
      (b.sail ? ` · voile ${esc(b.sail)}` : "") +
      `<br>${fmt(b.lat)}, ${fmt(b.lon)}` +
      `<br><span class="meta">Cap ${esc(cap)} · Yellowbrick</span>`
    );
  }

  function sdrPopup(k) {
    const dist = k.distance_km != null ? `${k.distance_km} km` : "";
    const snr = k.snr_hf != null ? `SNR HF ${k.snr_hf}` : "";
    const href = k.url ? `<br><a href="${esc(k.url)}" rel="noreferrer">Ouvrir le KiwiSDR</a>` : "";
    return (
      `<strong>${esc(k.name || "KiwiSDR")}</strong><br>` +
      `${esc(k.loc || "")}<br>` +
      [dist, snr].filter(Boolean).join(" · ") +
      href
    );
  }

  function sdrLabel(k) {
    if (k.loc) return String(k.loc);
    if (k.host) return String(k.host);
    const n = String(k.name || "KiwiSDR");
    const parts = n.split("|");
    return (parts[parts.length - 1] || n).trim().slice(0, 40);
  }

  function fmt(n) {
    return Number(n).toFixed(3);
  }

  function esc(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/"/g, "&quot;");
  }
})();

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
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · <a href="https://carto.com/attributions">CARTO</a> · positions <a href="https://yb.tl/ggr2026">Yellowbrick</a>',
    subdomains: "abcd",
    maxZoom: 18,
  }).addTo(map);

  const boatLayer = L.layerGroup();
  const sdrLayer = L.layerGroup();
  const bounds = [];

  const boatStyle = {
    radius: 6,
    color: "#c9a227",
    weight: 1,
    fillColor: "#e8c547",
    fillOpacity: 0.95,
  };
  const sdrStyle = {
    radius: 8,
    color: "#1e6b45",
    weight: 1,
    fillColor: "#3dba7a",
    fillOpacity: 0.95,
  };

  boats.forEach((b) => {
    const ll = [b.lat, b.lon];
    bounds.push(ll);
    const title = [b.name, b.sail].filter(Boolean).join(" · ");
    L.circleMarker(ll, boatStyle)
      .bindPopup(
        `<strong>${esc(title || "Bateau")}</strong><br>` +
          `${fmt(b.lat)}, ${fmt(b.lon)}<br>` +
          `<span class="meta">Yellowbrick</span>`
      )
      .addTo(boatLayer);
  });

  if (Number.isFinite(cent.lat) && Number.isFinite(cent.lon)) {
    const ll = [cent.lat, cent.lon];
    bounds.push(ll);
    L.circleMarker(ll, {
      radius: 11,
      color: "#c9a227",
      weight: 2,
      fillColor: "#081018",
      fillOpacity: 0.35,
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
    const dist = k.distance_km != null ? `${k.distance_km} km` : "";
    const snr = k.snr_hf != null ? `SNR HF ${k.snr_hf}` : "";
    const href = k.url ? `<br><a href="${esc(k.url)}" rel="noreferrer">Ouvrir le KiwiSDR</a>` : "";
    L.circleMarker(ll, sdrStyle)
      .bindPopup(
        `<strong>${esc(k.name || "KiwiSDR")}</strong><br>` +
          `${esc(k.loc || "")}<br>` +
          [dist, snr].filter(Boolean).join(" · ") +
          href
      )
      .addTo(sdrLayer);
  });

  boatLayer.addTo(map);
  sdrLayer.addTo(map);
  L.control
    .layers(
      null,
      { "Bateaux GGR": boatLayer, KiwiSDR: sdrLayer },
      { collapsed: false }
    )
    .addTo(map);

  if (bounds.length === 1) {
    map.setView(bounds[0], 5);
  } else if (bounds.length) {
    map.fitBounds(bounds, { padding: [28, 28], maxZoom: 8 });
  } else {
    map.setView([46.5, -1.79], 4);
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

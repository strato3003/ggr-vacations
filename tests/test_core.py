"""Tests géodésie, tracker Yellowbrick et classement KiwiSDR."""

from __future__ import annotations

import struct
from datetime import datetime, timezone

from recorder.fleet import parse_positions3, _heading_deg, _team_colour
from recorder.geo import centroid, fmt_latlon, haversine_km, initial_bearing
from recorder.kiwi_audio import ImaAdpcmDecoder, wav_from_snd_frames, _pcm_from_snd, _ws_uris
from recorder.kiwi_list import parse_kiwi_directory, score_kiwi
from recorder.session import next_vacation_utc, vacation_id


def test_haversine_les_sables_to_self():
    assert haversine_km(46.5025, -1.7888, 46.5025, -1.7888) < 0.01


def test_haversine_atlantic_order():
    # Les Sables → cap Finisterre ~ 550–700 km
    d = haversine_km(46.50, -1.79, 42.88, -9.27)
    assert 500 < d < 800


def test_initial_bearing_cardinals():
    assert abs(initial_bearing(0.0, 0.0, 1.0, 0.0) - 0.0) < 0.5
    assert abs(initial_bearing(0.0, 0.0, 0.0, 1.0) - 90.0) < 0.5


def test_centroid_and_fmt():
    c = centroid([(46.5, -1.8), (46.6, -1.7)])
    assert c is not None
    assert 46.5 < c[0] < 46.6
    assert -1.8 < c[1] < -1.7
    label = fmt_latlon(*c)
    assert label == "46.550°N 1.750°W"


def test_fmt_latlon_uses_hemispheres():
    assert fmt_latlon(46.55, -1.75) == "46.550°N 1.750°W"
    assert fmt_latlon(-33.9, 18.4).endswith("E")


def test_vacation_id_utc():
    dt = datetime(2026, 9, 7, 17, 50, tzinfo=timezone.utc)
    assert vacation_id(dt) == "2026-09-07T1750Z"


def test_next_vacation_before_slot():
    cfg = {"schedule": {"time_utc": "18:00", "lead_minutes": 10}}
    now = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
    nxt = next_vacation_utc(cfg, now)
    assert nxt.hour == 17 and nxt.minute == 50
    assert nxt.date() == now.date()


def test_scheduler_cron_is_1759_with_one_minute_lead():
    from recorder.scheduler import _lead

    assert _lead({"schedule": {"time_utc": "18:00", "lead_minutes": 1}}) == (17, 59)


def test_heading_from_two_fixes():
    moments = [
        {"lat": 46.50, "lon": -1.80, "at": 1},
        {"lat": 46.51, "lon": -1.80, "at": 2},
    ]
    h = _heading_deg(moments)
    assert h is not None
    assert abs(h - 0.0) < 2.0
    assert _team_colour({"colour": "FF3300"}) == "#FF3300"


def test_parse_positions3_single_fix():
    buf = bytearray()
    buf.append(0)
    buf += struct.pack(">I", 1_788_697_800)
    buf += struct.pack(">H", 6)  # Damien Guillou
    buf += struct.pack(">H", 1)
    buf += struct.pack(">I", 0)
    buf += struct.pack(">i", 4_650_250)
    buf += struct.pack(">i", -178_880)
    teams = parse_positions3(bytes(buf))
    assert len(teams) == 1
    assert teams[0]["id"] == 6
    fix = teams[0]["moments"][0]
    assert abs(fix["lat"] - 46.5025) < 1e-6
    assert abs(fix["lon"] + 1.7888) < 1e-6


def test_parse_kiwi_directory_trailing_comma():
    raw = """
var kiwisdr_com =
[
  {
    "id": "abc",
    "name": "test kiwi",
    "url": "http://example.invalid:8073"
  },
];
"""
    rows = parse_kiwi_directory(raw)
    assert len(rows) == 1
    assert rows[0]["id"] == "abc"


def test_score_prefers_closer_higher_snr():
    close = {"distance_km": 400, "snr_hf": 30, "free_slots": 4}
    far = {"distance_km": 8000, "snr_hf": 10, "free_slots": 1}
    assert score_kiwi(close, 0, 0) > score_kiwi(far, 0, 0)


def test_kiwi_snd_uri_uses_ws_kiwi_path():
    uris = _ws_uris({"host": "g3sdr.com", "port": 8074, "https": False})
    assert uris[0].startswith("ws://g3sdr.com:8074/ws/kiwi/")
    assert uris[0].endswith("/SND")
    assert "/ws/kiwi/" not in uris[1]


def test_pcm_from_snd_uncompressed_be():
    # flags, seq(3), smeter(2) puis un échantillon s16be = 0x0100
    body = bytes([0, 0, 0, 0, 0, 0x32, 0x00, 0x01, 0x00])
    pcm = _pcm_from_snd(body, ImaAdpcmDecoder())
    assert pcm == struct.pack("<h", 256)


def test_ima_adpcm_two_samples_per_byte():
    dec = ImaAdpcmDecoder()
    pcm = dec.decode(bytes([0x00]))
    assert len(pcm) == 4


def test_wav_from_snd_frames(tmp_path):
    body = bytes([0, 0, 0, 0, 0, 0x32, 0x00, 0x01, 0x00])
    dest = tmp_path / "t.wav"
    assert wav_from_snd_frames([b"SND" + body], dest)
    assert dest.stat().st_size > 44


def test_amplify_pcm_raises_quiet_peak():
    from recorder.kiwi_audio import amplify_pcm

    quiet = struct.pack("<h", 2000) * 64
    out = amplify_pcm(quiet)
    peak = max(abs(s) for s in struct.unpack("<" + "h" * 64, out))
    assert peak >= 20000


def test_kiwi_agc_uses_web_threshold():
    from recorder.kiwi_audio import _send_rx_setup
    import inspect

    src = inspect.getsource(_send_rx_setup)
    assert "thresh=-20" in src
    assert "thresh=-100" not in src


def test_recover_orphaned_clears_lock_and_running(tmp_path):
    import json

    from recorder.session import recover_orphaned

    cfg = {"storage": {"data_dir": str(tmp_path)}}
    lock = tmp_path / ".recording.lock"
    lock.write_text("2026-09-08T17:50:00+00:00", encoding="utf-8")
    folder = tmp_path / "vacations" / "2026-09-08T1750Z"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(
        json.dumps({"id": "2026-09-08T1750Z", "status": "running", "channels": []}),
        encoding="utf-8",
    )
    n = recover_orphaned(cfg)
    assert n >= 2
    assert not lock.exists()
    saved = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert saved["status"] == "error"
    assert "interrompu" in saved["error"]


def test_delete_vacation_removes_folder(tmp_path):
    from app.store import delete_vacation

    cfg = {"storage": {"data_dir": str(tmp_path)}}
    folder = tmp_path / "vacations" / "2026-09-11T0900Z-qrg"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text('{"id": "2026-09-11T0900Z-qrg", "status": "complete"}', encoding="utf-8")
    (folder / "audio-tx.wav").write_bytes(b"RIFF")
    assert delete_vacation("2026-09-11T0900Z-qrg", cfg) is None
    assert not folder.exists()
    assert delete_vacation("2026-09-11T0900Z-qrg", cfg) == "introuvable"
    assert delete_vacation("../etc", cfg) == "identifiant invalide"


def test_delete_vacation_refuses_running(tmp_path):
    from app.store import delete_vacation

    cfg = {"storage": {"data_dir": str(tmp_path)}}
    (tmp_path / ".recording.lock").write_text("now", encoding="utf-8")
    folder = tmp_path / "vacations" / "2026-09-11T0910Z"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(
        '{"id": "2026-09-11T0910Z", "status": "running"}', encoding="utf-8"
    )
    assert delete_vacation("2026-09-11T0910Z", cfg) == "enregistrement en cours"
    assert folder.exists()


def test_finalize_pending_promotes_orphan_with_audio(tmp_path):
    import json

    from recorder.session import finalize_pending_sessions

    cfg = {"storage": {"data_dir": str(tmp_path)}}
    folder = tmp_path / "vacations" / "2026-09-09T1750Z"
    folder.mkdir(parents=True)
    wav = folder / "audio-tx.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 80)
    (folder / "metadata.json").write_text(
        json.dumps(
            {
                "id": "2026-09-09T1750Z",
                "status": "error",
                "error": "Enregistrement interrompu (processus arrêté avant la fin)",
                "channels": [{"id": "tx", "audio_file": "audio-tx.wav"}],
            }
        ),
        encoding="utf-8",
    )
    assert finalize_pending_sessions(cfg) >= 1
    saved = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert saved["status"] == "complete"
    assert saved["channels"][0]["audio"] == "audio-tx.wav"
    assert "error" not in saved


def test_hold_page_stops_if_chromium_frozen(monkeypatch):
    import asyncio
    import time

    from recorder.screencast import _hold_page

    monkeypatch.setattr("recorder.screencast._PAGE_PING_S", 0.05)
    monkeypatch.setattr("recorder.screencast._PAGE_PING_TIMEOUT_S", 0.05)

    class Frozen:
        async def evaluate(self, _expr):
            raise RuntimeError("Target closed")

    async def go():
        t0 = time.monotonic()
        await _hold_page(Frozen(), 30)
        return time.monotonic() - t0

    elapsed = asyncio.run(go())
    assert elapsed < 2.0


def test_snd_hook_batches_instead_of_per_packet():
    from recorder.screencast import SND_HOOK_JS

    assert "ggrSndBatch" in SND_HOOK_JS
    assert "ggrSndFlush" in SND_HOOK_JS
    assert "ggrSndFrame" not in SND_HOOK_JS
    assert "fromCharCode.apply" in SND_HOOK_JS


def test_mux_cmd_itsoffset_delays_audio():
    from pathlib import Path

    from recorder.postprocess import mux_cmd

    cmd = mux_cmd(
        "ffmpeg",
        Path("v.webm"),
        Path("o.mp4"),
        audio_wav=Path("a.wav"),
        audio_delay_s=6.25,
    )
    assert cmd[cmd.index("-itsoffset") + 1] == "6.250"
    assert cmd.index("-i") < cmd.index("-itsoffset") < cmd.index("a.wav")


def test_channel_audio_delay_sidecar(tmp_path):
    from recorder.session import _channel_audio_delay

    (tmp_path / "audio-tx.delay").write_text("5.5\n", encoding="utf-8")
    assert _channel_audio_delay(tmp_path, {"id": "tx"}) == 5.5
    assert _channel_audio_delay(tmp_path, {"id": "tx", "audio_delay_s": 2}) == 2.0


def test_usb_dial_from_left_edge_of_ssb_blob():
    from recorder.kiwi_wf import (
        HUNT_CF_KHZ,
        HUNT_ZOOM,
        WF_BINS,
        bin_freq_khz,
        usb_dial_from_spectrum,
    )

    bins = [40.0] * WF_BINS
    # Voix USB 300–2700 Hz au-dessus de 14 200,0 kHz.
    i0 = min(
        range(WF_BINS),
        key=lambda i: abs(bin_freq_khz(i, HUNT_ZOOM, HUNT_CF_KHZ) - 14200.3),
    )
    i1 = min(
        range(WF_BINS),
        key=lambda i: abs(bin_freq_khz(i, HUNT_ZOOM, HUNT_CF_KHZ) - 14202.7),
    )
    for i in range(min(i0, i1), max(i0, i1) + 1):
        bins[i] = 180.0
    hit = usb_dial_from_spectrum(
        bins, zoom=HUNT_ZOOM, cf_khz=HUNT_CF_KHZ, low_hz=300, high_hz=2700
    )
    assert hit is not None
    assert abs(hit["freq_khz"] - 14200.0) < 0.2
    assert 1800 <= hit["width_hz"] <= 3000
    assert hit["usb_low_hz"] == 300 and hit["usb_high_hz"] == 2700


def test_usb_dial_rejects_cw_and_wide_am():
    from recorder.kiwi_wf import (
        HUNT_CF_KHZ,
        HUNT_ZOOM,
        WF_BINS,
        bin_freq_khz,
        usb_dial_from_spectrum,
    )

    def fill(f0: float, f1: float) -> list[float]:
        bins = [40.0] * WF_BINS
        i0 = min(range(WF_BINS), key=lambda i: abs(bin_freq_khz(i, HUNT_ZOOM, HUNT_CF_KHZ) - f0))
        i1 = min(range(WF_BINS), key=lambda i: abs(bin_freq_khz(i, HUNT_ZOOM, HUNT_CF_KHZ) - f1))
        for i in range(min(i0, i1), max(i0, i1) + 1):
            bins[i] = 180.0
        return bins

    assert usb_dial_from_spectrum(fill(14220.0, 14220.4), zoom=HUNT_ZOOM, cf_khz=HUNT_CF_KHZ) is None
    assert usb_dial_from_spectrum(fill(14200.0, 14208.0), zoom=HUNT_ZOOM, cf_khz=HUNT_CF_KHZ) is None


def test_usb_dial_none_on_flat_noise():
    from recorder.kiwi_wf import HUNT_CF_KHZ, HUNT_ZOOM, WF_BINS, usb_dial_from_spectrum

    assert usb_dial_from_spectrum([50.0] * WF_BINS, zoom=HUNT_ZOOM, cf_khz=HUNT_CF_KHZ) is None


def test_parse_wf_line_uncompressed():
    from recorder.kiwi_wf import WF_BINS, _parse_wf_line

    payload = bytes((i * 3) % 256 for i in range(WF_BINS))
    msg = b"W/F" + b"\x00" + struct.pack("<III", 0, 0, 7) + payload
    assert _parse_wf_line(msg) == list(payload)


def test_wf_uri_uses_ws_kiwi_path():
    from recorder.kiwi_audio import _ws_uris

    uris = _ws_uris({"host": "g3sdr.com", "port": 8074, "https": False}, "WF")
    assert uris[0].startswith("ws://g3sdr.com:8074/ws/kiwi/")
    assert uris[0].endswith("/WF")


def test_fmt_mhz_keeps_hertz():
    from recorder.config import fmt_mhz

    assert fmt_mhz(14135.0) == "14.135"
    assert fmt_mhz(16551.0) == "16.551"
    assert fmt_mhz(12418.0) == "12.418"
    assert fmt_mhz(16551.5) == "16.5515"


def test_parse_qrg_khz_accepts_mhz_or_khz():
    from recorder.config import parse_qrg_khz

    assert parse_qrg_khz(14.135) == 14135.0
    assert parse_qrg_khz(14135) == 14135.0
    assert parse_qrg_khz("16.551") == 16551.0
    assert parse_qrg_khz(16.551) == 16551.0
    assert parse_qrg_khz(12.418) == 12418.0


def test_zoom_for_span_covers_five_khz_window():
    from recorder.kiwi_wf import span_khz, zoom_for_span

    z = zoom_for_span(12.5)
    assert z == 11
    assert span_khz(z) >= 12.5
    assert span_khz(z + 1) < 12.5


def test_assign_vacation_kiwis_geo_sites():
    from recorder.kiwi_list import assign_vacation_kiwis

    def kiwi(kid, name, lat, lon, snr=20.0, free=3):
        return {
            "id": kid,
            "name": name,
            "lat": lat,
            "lon": lon,
            "snr_hf": snr,
            "free_slots": free,
            "url": f"http://{kid}.invalid",
        }

    fleet_lat, fleet_lon = -30.0, 10.0
    pool = [
        kiwi("loud-far", "singapore", 1.35, 103.82, snr=40, free=8),
        kiwi("near-a", "walvis", -29.5, 14.5, snr=12, free=3),
        kiwi("near-b", "cape", -33.92, 18.42, snr=18, free=2),
        kiwi("fr", "les-sables", 46.5025, -1.7888, snr=22, free=4),
        kiwi("th", "papeete", -17.5350, -149.5697, snr=16, free=2),
        kiwi("nz", "auckland", -36.85, 174.76, snr=30, free=4),
    ]
    cfg = {
        "sdr": {
            "min_free_slots": 2,
            "sites": {
                "france": {"lat": 46.5025, "lon": -1.7888, "label": "France", "radius_km": 1500},
                "tahiti": {"lat": -17.5350, "lon": -149.5697, "label": "Tahiti", "radius_km": 2500},
            },
        }
    }
    roles = assign_vacation_kiwis(pool, fleet_lat=fleet_lat, fleet_lon=fleet_lon, cfg=cfg)
    assert roles["tx"]["id"] == "near-a"
    assert roles["fleet"]["id"] == "near-b"
    assert roles["france"]["id"] == "fr"
    assert roles["tahiti"]["id"] == "th"
    assert "loud-far" not in {r["id"] for r in roles.values()}
    assert "nz" not in {r["id"] for r in roles.values()}


def test_ack_channels_per_site():
    from recorder.session import _channels, _pick_kiwis

    cfg = {
        "radio": {
            "tx": {"freq_khz": 14135.0, "label": "Bulletin météo F6KUF"},
            "ack": [
                {"freq_khz": 16551.0, "label": "Accusé 16,551 MHz"},
                {"freq_khz": 12418.0, "label": "Accusé 12,418 MHz"},
            ],
        },
        "sdr": {"screencast_tx": True, "screencast_ack": False},
    }
    sites = [
        {"id": "fleet", "label": "flotte"},
        {"id": "france", "label": "France"},
        {"id": "tahiti", "label": "Tahiti"},
    ]
    channels = _channels(cfg, sites)
    assert [c["id"] for c in channels] == [
        "tx",
        "ack1-fleet",
        "ack1-france",
        "ack1-tahiti",
        "ack2-fleet",
        "ack2-france",
        "ack2-tahiti",
    ]
    roles = {
        "tx": {"name": "k-tx"},
        "fleet": {"name": "k-fleet"},
        "france": {"name": "k-fr"},
        "tahiti": {"name": "k-th"},
    }
    got = _pick_kiwis(roles, channels)
    assert got["tx"]["name"] == "k-tx"
    assert got["ack1-france"]["name"] == "k-fr"
    assert got["ack2-tahiti"]["name"] == "k-th"


def test_runtime_settings_override_qrg(tmp_path, monkeypatch):
    monkeypatch.setenv("GGR_DATA_DIR", str(tmp_path))
    from recorder.config import load_config, qrg_context, save_runtime_settings

    save_runtime_settings(
        {
            "radio": {"tx": {"freq_khz": 14137.25, "qrg_tolerance_khz": 5.0}},
            "schedule": {"lead_minutes": 1, "duration_minutes": 12},
        }
    )
    qrg = qrg_context(load_config())
    assert qrg["tx_khz"] == 14137.25
    assert qrg["qrg_tolerance_khz"] == 5.0
    assert qrg["schedule_lead"] == 1
    assert qrg["duration_minutes"] == 12
    assert qrg["ack1_khz"] == 16551.0
    assert qrg["ack2_khz"] == 12418.0
    assert (tmp_path / "settings.json").is_file()


def test_legacy_ack_qrg_migrated_from_settings(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("GGR_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "radio": {
                    "ack": [
                        {"freq_khz": 16551.5, "label": "Accusé 16,5515 MHz"},
                        {"freq_khz": 12418.5, "label": "Accusé 12,4185 MHz"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    from recorder.config import load_config, qrg_context

    qrg = qrg_context(load_config())
    assert qrg["ack1_khz"] == 16551.0
    assert qrg["ack2_khz"] == 12418.0
    assert qrg["ack1_mhz"] == "16.551"
    assert qrg["ack2_mhz"] == "12.418"
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["radio"]["ack"][0]["freq_khz"] == 16551.0
    assert saved["radio"]["ack"][1]["freq_khz"] == 12418.0
    assert saved["radio"]["ack"][0]["label"] == "Accusé 16,551 MHz"


def test_usb_dial_in_plus_minus_five_khz_window():
    from recorder.kiwi_wf import WF_BINS, bin_freq_khz, usb_dial_from_spectrum, zoom_for_span

    cf = 14135.0
    tol = 5.0
    high_hz = 2700
    zoom = zoom_for_span((2 * tol + high_hz / 1000.0) * 1.25)
    bins = [40.0] * WF_BINS
    # Voix USB 300–2700 Hz au-dessus de 14 138,0 kHz (QRM +3 kHz).
    i0 = min(range(WF_BINS), key=lambda i: abs(bin_freq_khz(i, zoom, cf) - 14138.3))
    i1 = min(range(WF_BINS), key=lambda i: abs(bin_freq_khz(i, zoom, cf) - 14140.7))
    for i in range(min(i0, i1), max(i0, i1) + 1):
        bins[i] = 180.0
    hit = usb_dial_from_spectrum(
        bins,
        zoom=zoom,
        cf_khz=cf,
        lo_khz=cf - tol,
        hi_khz=cf + tol + high_hz / 1000.0,
        low_hz=300,
        high_hz=high_hz,
    )
    assert hit is not None
    assert abs(hit["freq_khz"] - 14138.0) < 0.25


def test_scheduler_buddy_cron_is_1159():
    from recorder.scheduler import _buddy_lead

    assert _buddy_lead({"buddy": {"time_utc": "12:00", "lead_minutes": 1}}) == (11, 59)


def test_fmt_khz_keeps_integer_channel():
    from recorder.config import fmt_khz

    assert fmt_khz(4483.0) == "4483"
    assert fmt_khz(6516.0) == "6516"


def test_covers_freqs_not_full_span():
    from recorder.kiwi_list import _covers_freqs, _covers_hf

    bands = (3_000_000, 8_000_000)
    assert _covers_freqs(bands, [4_483_000, 6_516_000])
    assert not _covers_hf(bands, 12_000_000, 17_000_000)
    assert not _covers_freqs(bands, [14_135_000])


def test_midday_prop_prefers_nvis_on_4mhz_and_hop_on_6mhz():
    from recorder.kiwi_list import hf_midday_prop_score, hf_midday_zone

    assert hf_midday_zone(400) == "nvis"
    assert hf_midday_zone(1100) == "skip"
    assert hf_midday_zone(2000) == "hop"
    assert hf_midday_prop_score(400, 4483) > hf_midday_prop_score(1100, 4483)
    assert hf_midday_prop_score(2000, 6516) > hf_midday_prop_score(1100, 6516)


def test_buddy_aim_trio_or_whole_fleet():
    from recorder.fleet import buddy_aim

    fleet = {
        "lat": 0.0,
        "lon": 0.0,
        "fmt": "0.000°N 0.000°E",
        "label": "flotte",
        "n_boats": 3,
        "boats": [
            {"id": 6, "name": "Damien Guillou", "lat": 40.0, "lon": -20.0},
            {"id": 13, "name": "Etienne Messikommer", "lat": 41.0, "lon": -21.0},
            {"id": 2, "name": "Louis Kerdelhue", "lat": 42.0, "lon": -22.0},
            {"id": 1, "name": "Gunnar Christensen", "lat": 10.0, "lon": 10.0},
        ],
        "source": "yellowbrick",
    }
    trio_cfg = {
        "buddy": {
            "centroid": {
                "skippers": ["Damien Guillou", "Etienne Messikommer", "Louis Kerdelhue"],
                "include_fleet": False,
            }
        }
    }
    trio = buddy_aim(fleet, trio_cfg)
    assert trio["n_boats"] == 3
    assert 40.0 < trio["lat"] < 42.0
    all_cfg = {
        "buddy": {
            "centroid": {
                "skippers": ["Damien Guillou"],
                "include_fleet": True,
            }
        }
    }
    whole = buddy_aim(fleet, all_cfg)
    assert whole["n_boats"] == 4
    assert whole["lat"] < trio["lat"]


def test_assign_buddy_kiwis_nvis_and_hop_not_just_nearest():
    from recorder.kiwi_list import assign_buddy_kiwis, score_buddy_kiwi

    def kiwi(kid, lat, lon, dist, snr=20, free=3):
        row = {
            "id": kid,
            "name": kid,
            "lat": lat,
            "lon": lon,
            "snr_hf": snr,
            "free_slots": free,
            "distance_km": dist,
            "bands_hz": [0, 30_000_000],
        }
        row["score"] = score_buddy_kiwi(row, 40.0, -20.0, [4483.0, 6516.0])
        return row

    # Centroïde ~ 40N 20W. Un Kiwi très proche mais en zone morte ~1100 km
    # ne doit pas gagner contre un NVIS et un 1 hop.
    pool = [
        kiwi("dead-zone", 50.0, -20.0, 1112, snr=35, free=8),
        kiwi("nvis", 41.0, -21.0, 140, snr=12, free=3),
        kiwi("hop-east", 40.0, 0.0, 1700, snr=18, free=4),
        kiwi("hop-west", 40.0, -40.0, 1700, snr=16, free=3),
    ]
    cfg = {"buddy": {"kiwi": {"count": 3, "min_separation_km": 300}}}
    roles = assign_buddy_kiwis(pool, lat=40.0, lon=-20.0, cfg=cfg)
    names = {k.get("name") for k in roles.values()}
    assert "nvis" in names
    assert "hop-east" in names or "hop-west" in names
    assert "dead-zone" not in names or len(names) >= 3
    assert roles["nvis"]["name"] == "nvis"

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

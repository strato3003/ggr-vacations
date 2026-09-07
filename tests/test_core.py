"""Tests géodésie, tracker Yellowbrick et classement KiwiSDR."""

from __future__ import annotations

import struct
from datetime import datetime, timezone

from recorder.fleet import parse_positions3
from recorder.geo import centroid, fmt_latlon, haversine_km
from recorder.kiwi_list import parse_kiwi_directory, score_kiwi
from recorder.session import next_vacation_utc, vacation_id


def test_haversine_les_sables_to_self():
    assert haversine_km(46.5025, -1.7888, 46.5025, -1.7888) < 0.01


def test_haversine_atlantic_order():
    # Les Sables → cap Finisterre ~ 550–700 km
    d = haversine_km(46.50, -1.79, 42.88, -9.27)
    assert 500 < d < 800


def test_centroid_and_fmt():
    c = centroid([(46.5, -1.8), (46.6, -1.7)])
    assert c is not None
    assert 46.5 < c[0] < 46.6
    assert "-1" in fmt_latlon(*c)


def test_vacation_id_utc():
    dt = datetime(2026, 9, 7, 17, 50, tzinfo=timezone.utc)
    assert vacation_id(dt) == "2026-09-07T1750Z"


def test_next_vacation_before_slot():
    cfg = {"schedule": {"time_utc": "18:00", "lead_minutes": 10}}
    now = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
    nxt = next_vacation_utc(cfg, now)
    assert nxt.hour == 17 and nxt.minute == 50
    assert nxt.date() == now.date()


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

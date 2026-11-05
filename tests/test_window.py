from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import window
from app.config import Settings
from app.window import Window, now_in_tz, parse_window

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.mark.parametrize("raw, start, end", [
    ("01:00-07:00", time(1, 0), time(7, 0)),
    ("22:00-06:00", time(22, 0), time(6, 0)),
    (" 00:30-23:59 ", time(0, 30), time(23, 59)),
    ("\t23:00-00:00\n", time(23, 0), time(0, 0)),
])
def test_parse_window(raw, start, end):
    assert parse_window(raw) == Window(start, end)


@pytest.mark.parametrize("raw", ["", "   "])
def test_parse_window_empty_is_none(raw):
    assert parse_window(raw) is None


@pytest.mark.parametrize("raw", ["01:00", "01:00-", "24:00-06:00", "01:60-02:00", "1-7",
                                 "01:00-07:00-08:00", "aa:bb-cc:dd", "01:00 07:00", "01.00-07.00",
                                 "1:05-7:00", "01:00 - 07:00", "01:00-\n07:00",
                                 "01:00-07:00\n08:00", "\u0660\u0661:00-07:00", "23:5-01:00"])
def test_parse_window_rejects_bad_format(raw):
    with pytest.raises(ValueError):
        parse_window(raw)


def test_parse_window_rejects_identical_times():
    with pytest.raises(ValueError):
        parse_window("05:00-05:00")


def test_window_is_frozen():
    w = Window(time(1), time(7))
    with pytest.raises(Exception):
        w.start = time(2)


def test_label_uses_en_dash():
    assert Window(time(1), time(7)).label == "01:00–07:00"
    assert Window(time(22, 30), time(6, 5)).label == "22:30–06:05"


def _at(h, m=0, day=6, tz=timezone.utc):
    return datetime(2026, 11, day, h, m, tzinfo=tz)


def test_contains_simple_range():
    w = Window(time(1), time(7))
    assert w.contains(_at(1)) is True          # start inclusive
    assert w.contains(_at(6, 59)) is True
    assert w.contains(_at(7)) is False         # end exclusive
    assert w.contains(_at(0, 59)) is False
    assert w.contains(_at(12)) is False


def test_contains_wrapping_range():
    w = Window(time(22), time(6))
    assert w.contains(_at(22)) is True
    assert w.contains(_at(23, 59)) is True
    assert w.contains(_at(0)) is True
    assert w.contains(_at(5, 59)) is True
    assert w.contains(_at(6)) is False
    assert w.contains(_at(12)) is False
    assert w.contains(_at(21, 59)) is False


def test_next_start_when_open_is_now():
    w = Window(time(1), time(7))
    now = _at(3)
    assert w.next_start(now) == now


def test_next_start_later_today_and_tomorrow():
    w = Window(time(1), time(7))
    assert w.next_start(_at(0, 30)) == _at(1)
    assert w.next_start(_at(8)) == _at(1, day=7)


def test_next_start_wrapping():
    w = Window(time(22), time(6))
    assert w.next_start(_at(12)) == _at(22)
    assert w.next_start(_at(6)) == _at(22)
    assert w.next_start(_at(23)) == _at(23)    # open


def test_next_start_across_spring_forward_in_berlin():
    # 2026-03-29: clocks go from 02:00 CET to 03:00 CEST.
    w = Window(time(1), time(7))
    nxt = w.next_start(datetime(2026, 3, 28, 23, 0, tzinfo=BERLIN))
    assert nxt.replace(tzinfo=None) == datetime(2026, 3, 29, 1, 0)
    assert nxt.utcoffset() == timedelta(hours=1)
    nxt = w.next_start(datetime(2026, 3, 29, 8, 0, tzinfo=BERLIN))
    assert nxt.replace(tzinfo=None) == datetime(2026, 3, 30, 1, 0)
    assert nxt.utcoffset() == timedelta(hours=2)
    # Inside the window on the DST day (04:00 CEST exists, wall time is what counts).
    assert w.contains(datetime(2026, 3, 29, 4, 0, tzinfo=BERLIN)) is True


def test_next_start_normalises_a_start_skipped_by_spring_forward():
    # 2026-03-29 in Berlin: 02:00..03:00 does not exist; a 02:30 start means 03:30 CEST.
    w = Window(time(2, 30), time(4))
    nxt = w.next_start(datetime(2026, 3, 29, 1, 0, tzinfo=BERLIN))
    assert nxt.replace(tzinfo=None) == datetime(2026, 3, 29, 3, 30)
    assert nxt.utcoffset() == timedelta(hours=2)
    assert nxt.astimezone(timezone.utc) == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
    assert w.contains(nxt) is True


def test_next_start_across_fall_back_in_berlin():
    # 2026-10-25: clocks go from 03:00 CEST back to 02:00 CET.
    w = Window(time(4), time(6))
    nxt = w.next_start(datetime(2026, 10, 24, 20, 0, tzinfo=BERLIN))
    assert nxt.replace(tzinfo=None) == datetime(2026, 10, 25, 4, 0)
    assert nxt.utcoffset() == timedelta(hours=1)
    assert nxt.astimezone(timezone.utc) == datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)


def test_to_dict_closed_and_open():
    w = Window(time(1), time(7))
    closed = w.to_dict(_at(12))
    assert closed == {"from": "01:00", "to": "07:00", "active": False,
                      "next_start": _at(1, day=7).isoformat()}
    assert w.to_dict(_at(2)) == {"from": "01:00", "to": "07:00", "active": True, "next_start": None}


def test_now_in_tz_uses_settings_tz():
    s = Settings.from_env({"TZ": "Europe/Berlin"})
    now = now_in_tz(s)
    assert now.tzinfo == BERLIN
    assert abs(now - datetime.now(timezone.utc)) < timedelta(seconds=5)


def test_now_in_tz_falls_back_to_utc_on_unknown_tz():
    s = Settings.from_env({"TZ": "Not/AZone"})
    assert window.now_in_tz(s).utcoffset() == timedelta(0)
    s = Settings.from_env({"TZ": ""})
    assert window.now_in_tz(s).utcoffset() == timedelta(0)

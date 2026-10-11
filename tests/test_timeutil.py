import datetime as dt

from app.timeutil import fmt, parse_clock, resolve_date, to_min

SUNDAY = dt.date(2026, 10, 11)


def test_minutes_round_trip():
    assert to_min("18:10") == 1090
    assert fmt(to_min("21:05")) == "21:05"
    assert fmt(to_min("24:30")) == "次日 00:30"


def test_resolve_date():
    assert resolve_date("週六", SUNDAY) == dt.date(2026, 10, 17)
    assert resolve_date("明天晚上", SUNDAY) == dt.date(2026, 10, 12)
    assert resolve_date("下週三", SUNDAY) == dt.date(2026, 10, 14)
    assert resolve_date("10/20 想看電影", SUNDAY) == dt.date(2026, 10, 20)
    assert resolve_date("2026-12-24", SUNDAY) == dt.date(2026, 12, 24)
    assert resolve_date("今天", SUNDAY) == SUNDAY
    assert resolve_date("隨便", SUNDAY) is None


def test_parse_clock():
    assert parse_clock("22:30", "home_by") == "22:30"
    assert parse_clock("10:30", "home_by") == "22:30"
    assert parse_clock("晚上十點半", "home_by") == "22:30"
    assert parse_clock("00:30", "home_by") == "24:30"
    assert parse_clock("3點", "depart_after") == "15:00"
    assert parse_clock("早一點出門", "depart_after") is None

import contextlib
import datetime as dt

import pytest
from pydantic import ValidationError

from app.collectors.base import Batch, Collector
from app.refresher import due_sources, next_check

NOW = dt.datetime(2026, 10, 12, 9, 0)


def test_everything_is_due_when_never_run():
    assert due_sources({"osm": None, "atmovies": None}, NOW) == ["osm", "atmovies"]


def test_atmovies_is_due_once_per_day():
    assert due_sources({"osm": NOW, "atmovies": NOW.replace(hour=6)}, NOW) == []
    assert due_sources({"osm": NOW, "atmovies": NOW - dt.timedelta(days=1)}, NOW) == ["atmovies"]


def test_osm_is_due_after_a_week():
    fresh = {"atmovies": NOW}
    assert due_sources(fresh | {"osm": NOW - dt.timedelta(days=6)}, NOW) == []
    assert due_sources(fresh | {"osm": NOW - dt.timedelta(days=7)}, NOW) == ["osm"]


def test_next_check():
    assert next_check(dt.datetime(2026, 10, 12, 5, 30), 6) == dt.datetime(2026, 10, 12, 6, 0)
    assert next_check(dt.datetime(2026, 10, 12, 6, 0), 6) == dt.datetime(2026, 10, 13, 6, 0)


class FakeOsm(Collector):
    name = "osm"

    def collect(self, start, days):
        return Batch()


class BrokenAtmovies(Collector):
    name = "atmovies"

    def collect(self, start, days):
        raise RuntimeError("網站掛了")


def test_run_due_runs_sources_and_remembers_errors(mem_session, monkeypatch):
    from app import refresher as mod

    made = []

    def fake_get_collector(name, session):
        made.append(name)
        return {"osm": FakeOsm(), "atmovies": BrokenAtmovies()}[name]

    monkeypatch.setattr(mod, "get_collector", fake_get_collector)
    r = mod.Refresher(session_factory=lambda: contextlib.nullcontext(mem_session), hour=6)

    assert r.run_due() == ["osm", "atmovies"]
    assert r.status()["errors"]["atmovies"].startswith("RuntimeError: 網站掛了")
    assert r.status()["running"] is None
    # osm 已經成功，不用再抓；atmovies 失敗，下一輪再試
    assert r.run_due() == ["atmovies"]
    assert made == ["osm", "atmovies", "atmovies"]


def test_status_endpoint_reports_refresh(client):
    data = client.get("/api/status").json()
    assert data["refresh"]["running"] is None
    assert data["refresh"]["auto"] is False


def test_an_exception_does_not_stop_the_other_sources(mem_session, monkeypatch):
    from app import refresher as mod

    def fake_get_collector(name, session):
        if name == "osm":
            raise KeyError("osm 壞了")
        return BrokenAtmovies()

    monkeypatch.setattr(mod, "get_collector", fake_get_collector)
    r = mod.Refresher(session_factory=lambda: contextlib.nullcontext(mem_session), hour=6)

    assert r.run_due() == ["osm", "atmovies"]
    errors = r.status()["errors"]
    assert errors["osm"] == "KeyError: 'osm 壞了'"
    assert errors["atmovies"].startswith("RuntimeError: 網站掛了")
    assert r.status()["running"] is None


def test_refresh_hour_must_be_a_real_hour():
    from app.config import Settings

    assert Settings(refresh_hour=0).refresh_hour == 0
    assert Settings(refresh_hour=23).refresh_hour == 23
    for bad in (-1, 24):
        with pytest.raises(ValidationError):
            Settings(refresh_hour=bad)

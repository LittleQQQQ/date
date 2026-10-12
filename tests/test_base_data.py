import datetime as dt

from sqlalchemy import func, select

from app.collectors.fixture import BaseCollector, FixtureCollector
from app.ingest import ensure_base_data, run_collector
from app.models import Area, Cinema, Movie, Restaurant, ScrapeRun, Showtime, TransitLink


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_base_collector_has_only_areas_and_transit():
    batch = BaseCollector().collect(dt.date(2026, 10, 12), 14)
    assert batch.areas and batch.stations and batch.transit_links
    assert not (batch.cinemas or batch.movies or batch.showtimes or batch.restaurants)
    assert batch.showtime_dates is None


def test_ensure_base_data_removes_demo_content(mem_session):
    run_collector(mem_session, FixtureCollector(), judge=False)
    assert _count(mem_session, Cinema) > 0

    run = ensure_base_data(mem_session)

    assert run.status == "ok", run.error
    for model in (Cinema, Movie, Showtime, Restaurant):
        assert _count(mem_session, model) == 0
    assert mem_session.get(Area, "台北市/信義") is not None
    assert _count(mem_session, TransitLink) > 0
    assert "fixture" not in {r.source for r in mem_session.scalars(select(ScrapeRun))}

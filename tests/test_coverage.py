import datetime as dt

import pytest

from app.collectors.coverage import AreaCoverage, Coverage, Point, load_coverage

XINYI = AreaCoverage("台北市/信義", "台北市", [Point(25.0410, 121.5655), Point(25.0330, 121.5637)])
XIMEN = AreaCoverage("台北市/西門町", "台北市", [Point(25.0421, 121.5081)])
COV = Coverage([XINYI, XIMEN])


def test_point_near_station_belongs_to_area():
    assert COV.area_for(25.0357, 121.5665) == "台北市/信義"  # 信義威秀
    assert COV.area_for(25.0446, 121.5074) == "台北市/西門町"


def test_point_far_from_every_station_is_outside():
    assert COV.area_for(24.9787, 121.5446) is None  # 新店


def test_nearest_area_wins_when_two_are_close():
    a = AreaCoverage("台北市/甲", "台北市", [Point(25.0000, 121.5000)])
    b = AreaCoverage("台北市/乙", "台北市", [Point(25.0000, 121.5060)])
    assert Coverage([a, b]).area_for(25.0000, 121.5050) == "台北市/乙"


def test_cities_and_get():
    assert COV.cities() == {"台北市"}
    assert COV.get("台北市/西門町") is XIMEN


def test_walk_to_cinemas_falls_back_to_stations():
    p = Point(25.0357, 121.5665)
    assert XINYI.walk_to_cinemas(p) == XINYI.walk_from_station(p)
    with_cinema = AreaCoverage("台北市/信義", "台北市", XINYI.stations, [Point(25.0357, 121.5665)])
    assert with_cinema.walk_to_cinemas(p) == 1


def _load_fixture(session):
    from app.collectors.fixture import FixtureCollector
    from app.ingest import upsert_batch

    upsert_batch(session, "fixture", FixtureCollector().collect(dt.date(2026, 10, 12), 1))


def test_load_coverage_from_database(mem_session):
    _load_fixture(mem_session)
    cov = load_coverage(mem_session)
    xinyi = cov.get("台北市/信義")
    assert len(xinyi.stations) == 2  # 市政府、台北101/世貿
    assert len(xinyi.cinemas) == 3
    assert {"台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市"} <= cov.cities()


def test_get_collector_passes_coverage(mem_session, monkeypatch):
    from app.collectors import COLLECTORS, Collector, get_collector

    class NeedsAreas(Collector):
        name = "needs-areas"
        needs_coverage = True

        def __init__(self, coverage):
            self.coverage = coverage

        def collect(self, start, days):
            raise NotImplementedError

    monkeypatch.setitem(COLLECTORS, NeedsAreas.name, NeedsAreas)
    _load_fixture(mem_session)
    collector = get_collector("needs-areas", mem_session)
    assert "台北市/信義" in {a.id for a in collector.coverage.areas}
    with pytest.raises(ValueError, match="session"):
        get_collector("needs-areas")

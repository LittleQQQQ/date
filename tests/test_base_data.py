import datetime as dt

from sqlalchemy import func, select

from app.collectors.base import (
    AreaRecord,
    Batch,
    CinemaRecord,
    MovieRecord,
    RestaurantRecord,
    ShowtimeRecord,
    StationRecord,
    TransitLinkRecord,
)
from app.collectors.fixture import BaseCollector, FixtureCollector
from app.ingest import ensure_base_data, run_collector, upsert_batch
from app.models import Area, Cinema, Movie, Restaurant, ScrapeRun, Showtime, Station, TransitLink


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


DAY = dt.date(2026, 10, 12)
OLD_AREA = "高雄市/巨蛋"


def _old_version(session):
    """模擬舊版資料檔匯入過的東西：一個後來刪掉的商圈（含車站、路段），以及爬蟲在那裡抓到的影城、場次、餐廳。"""
    upsert_batch(session, "base", Batch(
        areas=[AreaRecord(id=OLD_AREA, city="高雄市", name="巨蛋")],
        stations=[StationRecord(id="khh-old", name="舊站", city="高雄市", lat=22.6672, lng=120.3027, area_id=OLD_AREA)],
        transit_links=[
            TransitLinkRecord(from_station_id="khh-old", to_station_id="khh-main", mode="捷運", line="紅線",
                              ride_min=5, headway_min=8, first_train="06:00", last_train="24:00"),
            TransitLinkRecord(from_station_id="tnn-main", to_station_id="khh-main", mode="台鐵", line="台鐵縱貫線",
                              ride_min=35, headway_min=20, first_train="05:30", last_train="23:30"),
        ],
    ))
    places = {"old": OLD_AREA, "keep": "台北市/信義"}
    upsert_batch(session, "atmovies", Batch(
        cinemas=[CinemaRecord(external_id=k, name=f"影城{k}", area_id=a, lat=25.036, lng=121.566) for k, a in places.items()],
        movies=[MovieRecord(external_id="M", title="片", runtime_min=100)],
        showtimes=[
            ShowtimeRecord(external_id=f"{k}|M|{DAY}|18:00", cinema_external_id=k, movie_external_id="M",
                           raw_title="片", date=DAY, start="18:00", label="數位", price=300)
            for k in places
        ],
    ))
    upsert_batch(session, "osm", Batch(restaurants=[
        RestaurantRecord(external_id=k, name=f"餐廳{k}", area_id=a, lat=25.036, lng=121.566, description="餐廳",
                         price_per_person=500, hours={"default": ["11:30", "21:00"]})
        for k, a in places.items()
    ]))
    session.commit()


def test_base_import_removes_what_the_data_files_no_longer_have(mem_session):
    run_collector(mem_session, BaseCollector(), judge=False)
    _old_version(mem_session)

    run = run_collector(mem_session, BaseCollector(), judge=False)

    assert run.status == "ok", run.error
    assert mem_session.get(Area, OLD_AREA) is None
    assert mem_session.get(Station, "khh-old") is None
    links = {(l.from_station_id, l.to_station_id, l.line) for l in mem_session.scalars(select(TransitLink))}
    assert ("khh-old", "khh-main", "紅線") not in links
    assert ("tnn-main", "khh-main", "台鐵縱貫線") not in links
    assert {c.external_id for c in mem_session.scalars(select(Cinema))} == {"keep"}
    assert {r.external_id for r in mem_session.scalars(select(Restaurant))} == {"keep"}
    assert [s.external_id for s in mem_session.scalars(select(Showtime))] == [f"keep|M|{DAY}|18:00"]
    assert {k: run.counts[k] for k in (
        "stale_transit_links", "stale_stations", "stale_areas", "stale_cinemas", "stale_showtimes", "stale_restaurants",
    )} == {
        "stale_transit_links": 2, "stale_stations": 1, "stale_areas": 1,
        "stale_cinemas": 1, "stale_showtimes": 1, "stale_restaurants": 1,
    }
    # 資料檔裡有的都還在
    assert mem_session.get(Area, "高雄市/三多") is not None
    assert mem_session.get(Station, "khh-main") is not None


def test_fixture_import_does_not_remove_anything(mem_session):
    run_collector(mem_session, BaseCollector(), judge=False)
    _old_version(mem_session)

    run_collector(mem_session, FixtureCollector(), judge=False)

    assert mem_session.get(Area, OLD_AREA) is not None
    assert mem_session.get(Station, "khh-old") is not None

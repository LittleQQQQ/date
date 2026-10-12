import datetime as dt
import json

import pytest
from fakes import SAMPLES as ALL_SAMPLES
from fakes import FakeFetcher
from sqlalchemy import select

from app.collectors.base import ScrapeError
from app.collectors.coverage import AreaCoverage, Coverage, Point, load_coverage
from app.collectors.fixture import BaseCollector
from app.collectors.http import FetchError
from app.collectors.osm import MAX_RESTAURANTS_PER_AREA, OVERPASS_URL, OsmCollector, overpass_query
from app.ingest import run_collector
from app.models import Restaurant

SAMPLES = ALL_SAMPLES / "osm"
TODAY = dt.date(2026, 10, 12)
COV = Coverage([
    AreaCoverage("台北市/信義", "台北市", [Point(25.0410, 121.5655), Point(25.0330, 121.5637)]),
    AreaCoverage("台北市/西門町", "台北市", [Point(25.0421, 121.5081)]),
])
EMPTY = json.dumps({"elements": []})


def test_overpass_query_searches_around_each_station():
    query = overpass_query(COV.get("台北市/信義").stations)
    assert query.startswith("[out:json]")
    assert "around:865,25.041000,121.565500" in query and "around:865,25.033000,121.563700" in query
    assert '["amenity"~"^(restaurant|cafe|fast_food|bar|pub)$"]["name"]' in query
    assert query.rstrip().endswith("out center tags;")


def test_collect_builds_restaurants_and_tracks_areas():
    fetcher = FakeFetcher({OVERPASS_URL: [SAMPLES / "xinyi.json", FetchError("回應 504")]})
    batch = OsmCollector(COV, fetcher=fetcher).collect(TODAY, 1)
    assert batch.restaurant_areas == ["台北市/信義"]  # 西門町查詢失敗，保留舊資料
    assert batch.stats["failed_areas"] == 1
    by_id = {r.external_id: r for r in batch.restaurants}
    assert set(by_id) == {"node/1001", "node/1002", "node/1003", "way/2001"}
    ramen = by_id["node/1001"]
    assert ramen.hours == {"default": ["00:00", "24:00"]} and ramen.estimated == ["price"]
    assert ramen.description.startswith("日式餐廳")
    assert ramen.source_url == "https://www.openstreetmap.org/node/1001"
    hotpot = by_id["way/2001"]
    assert hotpot.estimated == ["hours", "price"] and hotpot.reservable and hotpot.price_per_person == 800
    assert (hotpot.lat, hotpot.lng) == (25.0398, 121.5683)
    assert by_id["node/1003"].hours == {"default": ["18:00", "26:00"]}
    assert "around:865,25.041000,121.565500" in fetcher.posts[0]["data"]


def test_all_areas_failing_is_an_error():
    fetcher = FakeFetcher({OVERPASS_URL: [FetchError("回應 504"), FetchError("回應 504")]})
    with pytest.raises(ScrapeError):
        OsmCollector(COV, fetcher=fetcher).collect(TODAY, 1)


def test_each_area_keeps_the_60_best():
    elements = [
        {"type": "node", "id": i, "lat": 25.0410 + i * 0.00001, "lon": 121.5655,
         "tags": {"amenity": "restaurant", "name": f"店{i:02d}"}}
        for i in range(70)
    ]
    elements[69]["tags"]["opening_hours"] = "Mo-Su 11:00-21:00"  # 最遠，但資料最完整
    fetcher = FakeFetcher({OVERPASS_URL: [json.dumps({"elements": elements}), EMPTY]})
    names = [r.name for r in OsmCollector(COV, fetcher=fetcher).collect(TODAY, 1).restaurants]
    assert len(names) == MAX_RESTAURANTS_PER_AREA
    assert names[0] == "店69"
    assert "店60" not in names  # 資料一樣少時，越遠越後面


def test_run_osm_into_database(mem_session):
    run_collector(mem_session, BaseCollector(), judge=False)
    xinyi = (SAMPLES / "xinyi.json").read_text(encoding="utf-8")
    # 只有信義的查詢（以市政府站為圓心）回傳樣本，其他商圈都是空的
    fetcher = FakeFetcher({OVERPASS_URL: lambda data: xinyi if "25.041000,121.565500" in data["data"] else EMPTY})
    run = run_collector(mem_session, OsmCollector(load_coverage(mem_session), fetcher=fetcher))
    assert run.status == "ok", run.error
    rows = mem_session.scalars(select(Restaurant).where(Restaurant.source == "osm")).all()
    assert len(rows) == 4 and all(r.area_id == "台北市/信義" for r in rows)


RUNTIME_ERROR = json.dumps({"elements": [], "remark": 'runtime error: Query timed out in "query" at line 3 after 61 seconds.'})


@pytest.mark.parametrize("bad", [RUNTIME_ERROR, "<html>busy</html>", "[]", "null"], ids=["runtime_error", "not_json", "list", "null"])
def test_unusable_answer_counts_as_a_failed_area(bad):
    fetcher = FakeFetcher({OVERPASS_URL: [bad, SAMPLES / "xinyi.json"]})  # 信義失敗，西門町正常
    batch = OsmCollector(COV, fetcher=fetcher).collect(TODAY, 1)
    assert batch.restaurant_areas == ["台北市/西門町"]
    assert batch.stats["failed_areas"] == 1
    assert [r.external_id for r in batch.restaurants] == ["node/1005"]


def test_runtime_error_keeps_the_old_restaurants(mem_session):
    run_collector(mem_session, BaseCollector(), judge=False)
    xinyi = (SAMPLES / "xinyi.json").read_text(encoding="utf-8")
    first = FakeFetcher({OVERPASS_URL: lambda data: xinyi if "25.041000,121.565500" in data["data"] else EMPTY})
    run_collector(mem_session, OsmCollector(load_coverage(mem_session), fetcher=first))
    # 第二次信義的查詢逾時（HTTP 200 但帶執行錯誤），不能把信義既有的店當成歇業刪掉
    second = FakeFetcher({OVERPASS_URL: lambda data: RUNTIME_ERROR if "25.041000,121.565500" in data["data"] else EMPTY})
    run = run_collector(mem_session, OsmCollector(load_coverage(mem_session), fetcher=second))
    assert run.status == "ok", run.error
    rows = mem_session.scalars(select(Restaurant).where(Restaurant.source == "osm")).all()
    assert len(rows) == 4 and all(r.area_id == "台北市/信義" for r in rows)

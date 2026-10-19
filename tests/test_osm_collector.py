"""OSM 餐廳爬蟲：用存下來的 Overpass 回應（台北信義一帶的樣本），不連網。"""
import json

import pytest
from fakes import SAMPLES as ALL_SAMPLES
from fakes import FakeFetcher
from sqlalchemy import func, select

from app.collectors.base import ScrapeError
from app.collectors.coverage import Coverage, CoveredStation
from app.collectors.http import FetchError
from app.collectors.osm import MAX_RESTAURANTS_PER_AREA, OVERPASS_URL, OsmCollector, overpass_query
from app.ingest import run_collector
from app.models import Restaurant

SAMPLES = ALL_SAMPLES / "osm"
XINYI = (SAMPLES / "xinyi.json").read_text(encoding="utf-8")
EMPTY = json.dumps({"elements": []})
COV = Coverage([
    CoveredStation("tpe-cityhall", "市政府", "xinyi", 25.0410, 121.5655),
    CoveredStation("tpe-taipei101", "台北101", "xinyi", 25.0330, 121.5637),
    CoveredStation("tpe-ximen", "西門", "ximen", 25.0421, 121.5081),
])
AT_XINYI = "25.041000,121.565500"  # 信義的查詢裡一定有市政府站


def by_area(xinyi, ximen=EMPTY):
    """依查詢裡的座標回應：信義回 xinyi，西門回 ximen（內容可以是例外，代表查詢失敗）。"""
    def answer(data):
        result = xinyi if AT_XINYI in data["data"] else ximen
        if isinstance(result, Exception):
            raise result
        return result
    return FakeFetcher({OVERPASS_URL: answer})


def test_overpass_query_searches_around_each_station():
    query = overpass_query(COV.by_area()["xinyi"])
    assert query.startswith("[out:json]")
    assert f"around:800,{AT_XINYI}" in query and "around:800,25.033000,121.563700" in query
    assert '["amenity"~"^(restaurant|cafe|fast_food|bar|pub)$"]["name"]' in query
    assert query.rstrip().endswith("out center tags;")


def test_collect_keeps_the_raw_osm_data():
    batch = OsmCollector(COV, fetcher=by_area(XINYI, FetchError("回應 504"))).collect()
    assert batch.stats["failed_areas"] == 1 and not batch.restaurants_complete  # 西門查詢失敗：不判斷收店
    by_id = {r.external_id: r for r in batch.restaurants}
    assert set(by_id) == {"node/1001", "node/1002", "node/1003", "way/2001"}
    ramen = by_id["node/1001"]
    assert ramen.osm_opening_hours == "24/7" and ramen.osm_tags["cuisine"].startswith("japanese")
    hotpot = by_id["way/2001"]
    assert (hotpot.lat, hotpot.lng) == (25.0398, 121.5683)  # way 用中心點
    assert hotpot.osm_opening_hours == "sunrise-sunset"  # 看不懂的寫法照樣存，排程時再估計


def test_all_areas_failing_is_an_error():
    with pytest.raises(ScrapeError):
        OsmCollector(COV, fetcher=by_area(FetchError("回應 504"), FetchError("回應 504"))).collect()


def test_each_area_keeps_the_60_best():
    elements = [
        {"type": "node", "id": i, "lat": 25.0410 + i * 0.00001, "lon": 121.5655,
         "tags": {"amenity": "restaurant", "name": f"店{i:02d}"}}
        for i in range(70)
    ]
    elements[69]["tags"]["opening_hours"] = "Mo-Su 11:00-21:00"  # 最遠，但資料最完整
    batch = OsmCollector(COV, fetcher=by_area(json.dumps({"elements": elements}))).collect()
    names = [r.osm_name for r in batch.restaurants]
    assert len(names) == MAX_RESTAURANTS_PER_AREA
    assert names[0] == "店69"
    assert "店60" not in names  # 資料一樣少時，越遠越後面
    assert batch.restaurants_complete


RUNTIME_ERROR = json.dumps({"elements": [], "remark": 'runtime error: Query timed out in "query" at line 3 after 61 seconds.'})


@pytest.mark.parametrize(
    "bad",
    [RUNTIME_ERROR, "<html>busy</html>", "[]", "null", '{"remark": "x"}'],
    ids=["runtime_error", "not_json", "list", "null", "no_elements"],
)
def test_unusable_answer_counts_as_a_failed_area(bad):
    batch = OsmCollector(COV, fetcher=by_area(bad, XINYI)).collect()  # 信義失敗，西門正常
    assert batch.stats["failed_areas"] == 1
    assert [r.external_id for r in batch.restaurants] == ["node/1005"]  # 樣本裡只有這家離西門最近


def test_run_osm_into_the_database(session):
    run = run_collector(session, OsmCollector(COV, fetcher=by_area(XINYI)))
    assert run.status == "success", run.error
    rows = session.scalars(select(Restaurant)).all()
    assert len(rows) == 4 and all(r.source == "osm" and r.is_active for r in rows)  # 離西門最近的那家不在信義
    assert all(r.area_id is None for r in rows)  # 區域由 Jev 判斷（第 5 步）


def test_runtime_error_keeps_the_old_restaurants(session):
    run_collector(session, OsmCollector(COV, fetcher=by_area(XINYI)))
    # 第二次信義的查詢逾時（HTTP 200 但帶執行錯誤），不能把信義既有的店當成收了
    run = run_collector(session, OsmCollector(COV, fetcher=by_area(RUNTIME_ERROR)))
    assert run.status == "success", run.error
    assert session.scalar(select(func.count()).select_from(Restaurant).where(Restaurant.is_active)) == 4

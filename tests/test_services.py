"""規劃要用的資料（services.py）：和資料庫有關的檢查、篩選、營業時間、路網。"""
import datetime as dt

import pytest
from plan_data import TOMORROW, at, build, request
from sqlalchemy import select

from app.models import Restaurant, Station, TransitFare, TransitLink
from app.schemas import PlanRequest
from app.services import PlanError, load_plan_context, load_router, osm_eatery
from app.timeutil import TW, to_min

pytestmark = pytest.mark.usefixtures("redis_client")


def load(session, locations, **overrides):
    return load_plan_context(session, PlanRequest.model_validate(request(locations, **overrides)))


def test_context_has_places_showtimes_and_restaurants(session):
    ctx = load(session, build(session))
    assert ctx.origin.name == "中壢車站" and ctx.day == TOMORROW and ctx.depart == to_min("17:00")
    assert [s.start for s in ctx.showtimes] == [to_min("18:00"), to_min("20:00")]
    [ramen] = ctx.eateries
    assert ramen.place.name == "一樂拉麵" and (ramen.open, ramen.close) == (to_min("11:00"), to_min("22:00"))
    assert not ramen.hours_estimated and ramen.price_level == "MODERATE"


@pytest.mark.parametrize(
    ("overrides", "code", "message"),
    [
        ({"origin_location_id": "bade"}, "VALIDATION_ERROR", "不在中壢"),
        ({"depart_at": (dt.datetime.now(TW) - dt.timedelta(hours=1)).isoformat()}, "VALIDATION_ERROR", "晚於現在"),
        ({"depart_at": at("17:00", TOMORROW + dt.timedelta(days=5)).isoformat()}, "VALIDATION_ERROR", "只到"),
        ({"origin_location_id": 999999}, "NOT_FOUND", "找不到"),
    ],
)
def test_invalid_requests(session, overrides, code, message):
    locations = build(session)
    overrides = {k: locations[v] if v in locations else v for k, v in overrides.items()} if "origin_location_id" in overrides else overrides
    with pytest.raises(PlanError, match=message) as exc:
        load(session, locations, **overrides)
    assert exc.value.code == code


def test_area_without_showtimes(session):
    locations = build(session)
    with pytest.raises(PlanError) as exc:
        load(session, locations, area="bade", origin_location_id=locations["bade"], destination_location_id=locations["bade"])
    assert exc.value.code == "NO_SHOWTIMES"


def test_filters_by_genre_and_cuisine(session):
    locations = build(session)
    assert load(session, locations, movie_genre="romance").showtimes == []
    assert load(session, locations, cuisine="korean").eateries == []
    assert len(load(session, locations, movie_genre="action", cuisine="japanese").eateries) == 1


def test_osm_hours_follow_the_weekday(session):
    build(session)
    rest = session.scalar(select(Restaurant))
    rest.osm_tags = {**rest.osm_tags, "opening_hours": "Mo-Fr 11:00-21:00; Sa 12:00-23:00; Su off"}
    saturday = dt.date(2026, 10, 24)
    assert (osm_eatery(rest, 24.96, 121.22, saturday).open, osm_eatery(rest, 24.96, 121.22, saturday).close) == (720, 1380)
    assert osm_eatery(rest, 24.96, 121.22, saturday + dt.timedelta(days=1)).open is None  # 星期日公休
    rest.osm_tags = {"amenity": "cafe"}  # 沒標營業時間：依店的類型估計
    cafe = osm_eatery(rest, 24.96, 121.22, saturday)
    assert cafe.hours_estimated and cafe.hours_text.endswith("（估計）")


def test_router_prefers_tdx_links_and_carries_fares(session):
    build(session)
    ids = dict(session.execute(select(Station.code, Station.id)).all())
    session.add(TransitLink(from_station_id=ids["tyn-zhongli"], to_station_id=ids["tyn-sogo"], mode="公車", line="公車（170）",
                            ride_min=4, headway_min=30, first_min=390, last_min=1260, source="tdx"))
    session.add(TransitFare(from_station_id=ids["tyn-zhongli"], to_station_id=ids["tyn-sogo"], line="公車（170）", fare=18, source="tdx"))
    session.flush()
    router = load_router(session)
    [edge] = [e for e in router.graph.out_edges["tyn-zhongli"] if e.to_id == "tyn-sogo"]
    assert (edge.line, edge.fare) == ("公車（170）", 18)  # 同一段有 TDX 的，手寫的估計值就不用
    [back] = [e for e in router.graph.out_edges["tyn-sogo"] if e.to_id == "tyn-zhongli"]
    assert (back.line, back.fare) == ("公車（中壢車站—SOGO）", None)  # 反方向沒有 TDX：沿用手寫的，車資用估計

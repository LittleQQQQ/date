"""Google Places（規格書 3.3、4.5、7.1、7.2）：用假的 HTTP 回應測，不連網、不耗額度。

- 呼叫：欄位遮罩（決定計費等級）、金鑰、每月上限、錯誤
- 每週找餐廳：只存 place_id 和座標，Jev 判斷類別和區域
- 座標更新：25 天更新、店不在了清掉、超過 30 天更新不到也清掉
- 規劃：候選的 Google 餐廳補店家資料，查不到或公休就換下一個
"""
import datetime as dt
import json
from dataclasses import replace

import httpx
import pytest
from plan_data import build, request
from sqlalchemy import func, select

from app import services
from app.collectors.google_places import (
    DETAILS_FIELDS, LOCATION_FIELDS, NEARBY_FIELDS, GooglePlaces, PlaceNotFound, PlacesError, QuotaExhausted, parse_hours,
    to_tags,
)
from app.config import settings
from app.google_sync import google_eatery, make_enricher, refresh_google_geoms, refresh_google_places
from app.ingest import load_network, load_seed
from app.jev.mock import MockJev
from app.jobs import refresh_job
from app.jobs.refresh_job import SKIPPED
from app.models import Area, CrawlRun, JevJudgment, Restaurant, lat_of, point_wkt
from app.planner import MAX_DETAILS_PER_PLAN, Eatery, PlanContext, ShowInfo, plan
from app.routing import Place, Router
from app.schemas import PlanRequest
from app.timeutil import now_tw, to_min
from app.transit import TransitGraph

pytestmark = pytest.mark.usefixtures("redis_client")

ZHONGLI_STATION = (24.9536, 121.2257)
RAMEN = {
    "id": "ChIJramen", "displayName": {"text": "麵屋一燈"}, "primaryType": "ramen_restaurant",
    "types": ["ramen_restaurant", "japanese_restaurant", "restaurant", "food"],
    "formattedAddress": "桃園市中壢區中和路1號", "location": {"latitude": 24.9540, "longitude": 121.2262},
}
CAFE = {
    "id": "ChIJcafe", "displayName": {"text": "巷子咖啡"}, "primaryType": "cafe", "types": ["cafe", "food"],
    "formattedAddress": "桃園市中壢區新興路2號", "location": {"latitude": 24.9545, "longitude": 121.2250},
}
# 星期一到五 11:00–21:00、星期六 11:00–23:00、星期日公休（Google 的星期 0 是星期日）
WEEK = {"periods": [
    {"open": {"day": d, "hour": 11, "minute": 0}, "close": {"day": d, "hour": 23 if d == 6 else 21, "minute": 0}}
    for d in range(1, 7)
]}
SATURDAY = dt.date(2026, 10, 24)


class FakeGoogle:
    """假的 Places API：記下每個請求，依網址和欄位遮罩回應。"""

    def __init__(self, nearby=None, details=None, locations=None, status=None):
        self.nearby = nearby or {}  # 車站座標附近 → 店家
        self.details = details or {}
        self.locations = locations or {}
        self.status = status or {}  # place_id → HTTP 狀態碼
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith(":searchNearby"):
            center = json.loads(request.content)["locationRestriction"]["circle"]["center"]
            for (lat, lng), places in self.nearby.items():
                if abs(center["latitude"] - lat) < 1e-4 and abs(center["longitude"] - lng) < 1e-4:
                    return httpx.Response(200, json={"places": places})
            return httpx.Response(200, json={})  # 附近沒有店：Google 回空物件
        place_id = request.url.path.rsplit("/", 1)[-1]
        if code := self.status.get(place_id):
            return httpx.Response(code, json={"error": {"code": code, "message": "fake", "status": "FAKE"}})
        if request.headers["X-Goog-FieldMask"] == "location":
            if place_id not in self.locations:
                return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND"}})
            lat, lng = self.locations[place_id]
            return httpx.Response(200, json={"location": {"latitude": lat, "longitude": lng}})
        if place_id not in self.details:
            return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND"}})
        return httpx.Response(200, json=self.details[place_id])


def client(fake: FakeGoogle, redis_client, api_key: str = "test-key") -> GooglePlaces:
    return GooglePlaces(api_key=api_key, client=httpx.Client(transport=httpx.MockTransport(fake)), redis_client=redis_client,
                        sleep=lambda seconds: None)  # 重試不用真的等


# ---------- 呼叫 ----------


def test_nearby_search_asks_only_for_pro_fields_and_counts_quota(redis_client):
    fake = FakeGoogle(nearby={ZHONGLI_STATION: [RAMEN, CAFE, {"id": "ChIJnoloc"}]})
    google = client(fake, redis_client)
    places = google.search_nearby(*ZHONGLI_STATION, 800)
    assert [p["id"] for p in places] == ["ChIJramen", "ChIJcafe"]  # 沒有座標的不要
    [req] = fake.requests
    assert req.headers["X-Goog-Api-Key"] == "test-key"
    assert req.headers["X-Goog-FieldMask"] == ",".join(NEARBY_FIELDS)
    body = json.loads(req.content)
    assert body["maxResultCount"] == 20 and body["locationRestriction"]["circle"]["radius"] == 800.0
    assert google.nearby_quota.used() == 1 and google.details_quota.used() == 0


def test_details_and_location_use_their_own_field_masks_and_quotas(redis_client):
    fake = FakeGoogle(details={"ChIJramen": RAMEN}, locations={"ChIJramen": (24.95, 121.22)})
    google = client(fake, redis_client)
    assert google.details("ChIJramen")["displayName"]["text"] == "麵屋一燈"
    assert google.location("ChIJramen") == (24.95, 121.22)
    assert [r.headers["X-Goog-FieldMask"] for r in fake.requests] == [",".join(DETAILS_FIELDS), ",".join(LOCATION_FIELDS)]
    assert fake.requests[0].url.params["languageCode"] == "zh-TW"
    assert (google.details_quota.used(), google.location_quota.used()) == (1, 1)


def test_monthly_limit_stops_calls_before_they_are_made(redis_client, monkeypatch):
    monkeypatch.setattr(settings, "google_details_monthly_limit", 2)
    fake = FakeGoogle(details={"ChIJramen": RAMEN})
    google = client(fake, redis_client)
    google.details("ChIJramen")
    google.details("ChIJramen")
    with pytest.raises(QuotaExhausted):
        google.details("ChIJramen")
    assert len(fake.requests) == 2 and google.details_quota.remaining() == 0


def test_errors(redis_client):
    fake = FakeGoogle(status={"ChIJforbidden": 403})
    google = client(fake, redis_client)
    with pytest.raises(PlaceNotFound):
        google.details("ChIJgone")
    assert google.details_quota.used() == 1  # 404 也算一次
    with pytest.raises(PlacesError, match="沒有開放 Places API"):
        google.details("ChIJforbidden")
    assert google.details_quota.used() == 1  # 金鑰或帳單有問題：Google 不計費
    with pytest.raises(PlacesError, match="沒有設定"):
        client(fake, redis_client, api_key="").details("ChIJramen")
    assert len(fake.requests) == 2  # 沒有金鑰就不送出


def test_server_errors_are_retried_and_counted_once(redis_client):
    codes, waits = [503, 500, 200], []

    def handler(request):
        code = codes.pop(0)
        return httpx.Response(code, json={"displayName": {"text": "麵屋一燈"}} if code == 200 else {})

    google = GooglePlaces(api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(handler)),
                          redis_client=redis_client, sleep=waits.append)
    assert google.details("ChIJramen")["displayName"]["text"] == "麵屋一燈"
    assert waits == [1.0, 2.0] and google.details_quota.used() == 1  # 重試成功只算一次
    codes[:] = [503, 503, 503]
    with pytest.raises(PlacesError, match="伺服器錯誤"):
        google.details("ChIJramen")
    assert google.details_quota.used() == 1  # 失敗不計


def test_types_become_osm_like_tags():
    assert to_tags(RAMEN) == {"amenity": "restaurant", "cuisine": "ramen;japanese", "name": "麵屋一燈"}
    assert to_tags(CAFE)["amenity"] == "cafe"
    assert to_tags({"types": ["bar", "restaurant"]})["amenity"] == "bar"


def test_opening_hours():
    assert parse_hours(WEEK) == {"default": ["11:00", "21:00"], "sat": ["11:00", "23:00"], "sun": None}
    assert parse_hours({"periods": [{"open": {"day": 0, "hour": 0, "minute": 0}}]}) == {"default": ["00:00", "24:00"]}
    # 跨午夜：星期五 18:00 到星期六 02:00 → 18:00–26:00
    late = {"periods": [{"open": {"day": 5, "hour": 18}, "close": {"day": 6, "hour": 2}}]}
    assert parse_hours(late)["fri"] == ["18:00", "26:00"]
    # 分段營業取晚餐時段
    split = {"periods": [{"open": {"day": d, "hour": h}, "close": {"day": d, "hour": h + 3}} for d in range(7) for h in (11, 17)]}
    assert parse_hours(split) == {"default": ["17:00", "20:00"]}
    assert parse_hours(None) is None
    assert parse_hours({"periods": [{"open": {"day": 1, "hour": 11}, "close": {"hour": 21}}]}) is None  # 看不懂：改用估計值


# ---------- 每週找餐廳 ----------


class CountingJev(MockJev):
    def __init__(self):
        self.states: list[str] = []

    def ask(self, state, questions):
        self.states.append(state)
        return super().ask(state, questions)


def test_weekly_search_stores_only_place_id_and_coordinates(session, redis_client):
    load_network(session)
    load_seed(session)
    fake = FakeGoogle(nearby={ZHONGLI_STATION: [RAMEN, CAFE], (24.962084, 121.2252): [RAMEN]})
    jev = CountingJev()
    stats = refresh_google_places(session, client(fake, redis_client), jev)
    assert stats["places"] == 2 and stats["failed_searches"] == 0
    rows = session.execute(
        select(Restaurant.external_id, Restaurant.osm_name, Restaurant.osm_tags, Restaurant.category_code, Area.code)
        .join(Area, Area.id == Restaurant.area_id).where(Restaurant.source == "google").order_by(Restaurant.external_id)
    ).all()
    assert rows == [("ChIJcafe", None, None, "cafe_dessert", "zhongli"), ("ChIJramen", None, None, "japanese", "zhongli")]
    # 店名只在判斷時用到
    assert any("麵屋一燈" in s for s in jev.states)
    assert session.scalar(select(func.count()).select_from(JevJudgment).where(JevJudgment.subject_type == "restaurant")) == 4

    # 下週再找一次：已經判斷過的不再問 Jev
    jev.states.clear()
    refresh_google_places(session, client(fake, redis_client), jev)
    assert jev.states == []
    assert session.scalar(select(func.count()).select_from(Restaurant).where(Restaurant.source == "google")) == 2


def test_weekly_search_fails_when_every_search_fails(session, redis_client, monkeypatch):
    from app.collectors.base import ScrapeError

    load_network(session)
    monkeypatch.setattr(settings, "google_nearby_monthly_limit", 0)
    with pytest.raises(ScrapeError, match="額度用完"):
        refresh_google_places(session, client(FakeGoogle(), redis_client), MockJev())


# ---------- 座標更新 ----------


def test_coordinates_are_refreshed_or_cleared(session, redis_client):
    now = now_tw()

    def google_row(place_id, days_old):
        rest = Restaurant(source="google", external_id=place_id, geom=point_wkt(24.95, 121.22),
                          geom_fetched_at=now - dt.timedelta(days=days_old))
        session.add(rest)
        return rest

    fresh, moved, gone = google_row("ChIJfresh", 10), google_row("ChIJmoved", 26), google_row("ChIJgone", 26)
    flaky, stale = google_row("ChIJflaky", 27), google_row("ChIJstale", 31)
    session.flush()
    fake = FakeGoogle(locations={"ChIJmoved": (24.96, 121.23)}, status={"ChIJflaky": 503, "ChIJstale": 503})
    google = client(fake, redis_client)
    stats = refresh_google_geoms(session, google, now)
    assert stats == {"refreshed": 1, "gone": 1, "expired": 1, "kept": 1}
    assert "ChIJfresh" not in {r.url.path.rsplit("/", 1)[-1] for r in fake.requests}
    assert google.location_quota.used() == 2  # 成功的和 404 的
    assert session.scalar(select(lat_of(Restaurant.geom)).where(Restaurant.id == moved.id)) == pytest.approx(24.96)
    assert moved.geom_fetched_at == now and moved.is_active
    for rest in (gone, stale):  # 清掉座標、設為無效
        assert (rest.geom, rest.geom_fetched_at, rest.is_active) == (None, None, False)
    assert flaky.is_active and flaky.geom is not None and fresh.is_active


# ---------- 規劃時補店家資料 ----------


def placeholder(id_=1, place_id="ChIJramen", lat=24.9585):
    rest = Restaurant(id=id_, source="google", external_id=place_id, category_code="japanese")
    return google_eatery(rest, lat, 121.2257)


def test_enricher_fills_in_shop_details_for_that_day(redis_client):
    details = {"displayName": {"text": "麵屋一燈"}, "formattedAddress": "桃園市中壢區中和路1號",
               "regularOpeningHours": WEEK, "priceLevel": "PRICE_LEVEL_INEXPENSIVE"}
    google = client(FakeGoogle(details={"ChIJramen": details}), redis_client)
    saturday = make_enricher(google, SATURDAY)(placeholder())
    assert saturday.place.name == "麵屋一燈" and saturday.address == "桃園市中壢區中和路1號" and saturday.enriched
    assert (saturday.open, saturday.close, saturday.hours_text) == (to_min("11:00"), to_min("23:00"), "11:00–23:00")
    assert (saturday.price_level, saturday.price_per_person, saturday.hours_estimated) == ("INEXPENSIVE", 250, False)
    sunday = make_enricher(google, SATURDAY + dt.timedelta(days=1))(placeholder())
    assert sunday.open is None and sunday.hours_text == "公休"
    no_hours = client(FakeGoogle(details={"ChIJramen": {"displayName": {"text": "麵屋一燈"}}}), redis_client)
    estimated = make_enricher(no_hours, SATURDAY)(placeholder())
    assert estimated.hours_estimated and estimated.hours_text.endswith("（估計）") and estimated.price_per_person == 500
    assert make_enricher(google, SATURDAY)(placeholder(place_id="ChIJgone")) is None


HOME = Place("loc1", "中壢車站", 24.9536, 121.2257)
CINEMA = Place("cin1", "威尼斯影城", 24.9580, 121.2257)


def osm(id_):
    return Eatery(id_, Place(f"res{id_}", f"店{id_}", 24.9585, 121.2257), "osm", "japanese", to_min("11:00"),
                  to_min("22:00"), "11:00–22:00", False, 400, "MODERATE")


def planning(eateries, enrich):
    """先吃飯再看 20:00 的電影：用餐時段在估計的營業時間（11:30–21:00）內，候選才會輪到補資料。"""
    show = ShowInfo(1, CINEMA, "特務任務", "數位", to_min("20:00"), to_min("22:00"), 300)
    return PlanContext(SATURDAY, to_min("17:00"), HOME, HOME, "meal_first", 2, 3000, [show], eateries,
                       Router(TransitGraph([], []), [], use_cache=False), "google", enrich)


def test_plan_uses_real_details_for_google_candidates():
    calls, events = [], []

    def enrich(e):
        calls.append(e.id)
        return replace(e, place=replace(e.place, name="麵屋一燈"), open=to_min("11:00"), close=to_min("23:00"),
                       hours_text="11:00–23:00", hours_estimated=False, enriched=True)

    result = plan(planning([placeholder()], enrich), MockJev(), lambda stage, done, total: events.append(stage))
    [meal] = [i for i in result["plans"][0]["timeline"] if i["type"] == "meal"]
    assert meal["name"] == "麵屋一燈" and meal["attribution"] == "Google Maps" and meal["opening_hours_today"] == "11:00–23:00"
    assert calls == [1] and "fetching_places" in events


def test_plan_skips_google_candidates_without_details_or_closed():
    closed = lambda e: replace(e, open=None, close=None, hours_text="公休", enriched=True)  # noqa: E731
    for enrich in (lambda e: None, closed):
        result = plan(planning([placeholder(1), osm(2)], enrich), MockJev())
        assert {i["name"] for p in result["plans"] for i in p["timeline"] if i["type"] == "meal"} == {"店2"}
    # 晚上才開的店：補完資料用真的營業時間重新排，接不上就不用
    late = lambda e: replace(e, open=to_min("19:00"), close=to_min("26:00"), enriched=True)  # noqa: E731
    assert plan(planning([placeholder(1)], late), MockJev())["plans"] == []


def test_plan_stops_fetching_details_after_the_cap():
    calls = []
    eateries = [placeholder(i, f"ChIJ{i}") for i in range(1, 30)]
    result = plan(planning(eateries, lambda e: calls.append(e.id)), MockJev())
    assert result["plans"] == [] and len(calls) == MAX_DETAILS_PER_PLAN


# ---------- 規劃要用的資料 ----------


def add_google_restaurant(session):
    zhongli = session.scalar(select(Area.id).where(Area.code == "zhongli"))
    session.add(Restaurant(source="google", external_id="ChIJramen", geom=point_wkt(24.9623, 121.2250), area_id=zhongli,
                           category_code="japanese", geom_fetched_at=now_tw()))
    session.add(Restaurant(source="google", external_id="ChIJexpired", geom=None, area_id=zhongli,
                           category_code="japanese", is_active=False))
    session.flush()


def test_google_restaurants_join_only_with_a_key_and_quota(session, redis_client, monkeypatch):
    locations = build(session)
    add_google_restaurant(session)
    req = PlanRequest.model_validate(request(locations))

    ctx = services.load_plan_context(session, req)  # 沒有金鑰
    assert [e.source for e in ctx.eateries] == ["osm"] and ctx.enrich is None and ctx.restaurant_source == "osm"

    monkeypatch.setattr(settings, "google_places_api_key", "test-key")
    google = client(FakeGoogle(), redis_client)
    monkeypatch.setattr(services, "google_client", lambda: google)
    ctx = services.load_plan_context(session, req)
    assert [e.source for e in ctx.eateries] == ["osm", "google"]
    assert ctx.eateries[1].external_id == "ChIJramen" and ctx.enrich is not None and ctx.restaurant_source == "google"

    google.details_quota.consume(settings.google_details_monthly_limit)  # Enterprise 額度用完：只用 OSM
    assert [e.source for e in services.load_plan_context(session, req).eateries] == ["osm"]


# ---------- 背景工作 ----------


def test_job_skips_without_key_and_records_runs_with_one(monkeypatch, committed):
    assert refresh_job.refresh_google_places() == SKIPPED
    monkeypatch.setattr(settings, "google_places_api_key", "test-key")
    monkeypatch.setattr(refresh_job.google_sync, "refresh_google_places", lambda session: {"places": 0})
    assert refresh_job.refresh_google_places() == {"google_nearby": "success"}
    with refresh_job.SessionLocal() as session:
        assert session.scalar(select(CrawlRun.job).order_by(CrawlRun.id.desc())) == "google_nearby"


def test_manual_command_refuses_without_key(database):
    from app.ingest import main

    with pytest.raises(SystemExit, match="GOOGLE_PLACES_API_KEY"):
        main(["google"])

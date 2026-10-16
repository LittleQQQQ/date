import datetime as dt
import json
import re

import httpx
import pytest
from fakes import SAMPLES
from sqlalchemy import func, select

from app import google_places, services
from app.collectors.coverage import AreaCoverage, Point
from app.config import settings
from app.google_places import PlacesClient, PlacesError, parse_hours, place_price, to_record
from app.models import ApiUsage, Cinema, JevJudgment, Restaurant
from app.schemas import PlanRequest

PLACES = json.loads((SAMPLES / "google" / "xinyi.json").read_text(encoding="utf-8"))["places"]
BY_ID = {p["id"]: p for p in PLACES}
OPEN_NAMES = {"巷口拉麵", "微光義式餐館", "夜貓小酒館", "整夜漢堡", "森林咖啡"}
XINYI = AreaCoverage(
    "台北市/信義", "台北市", [Point(25.0410, 121.5655), Point(25.0330, 121.5637)], [Point(25.0359, 121.5670)]
)


def fake_client(handler) -> PlacesClient:
    return PlacesClient("test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))


def sample_handler(requests: list):
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"places": PLACES})

    return handle


# ---------- 格式轉換 ----------


def test_lunch_and_dinner_keeps_dinner_and_missing_days_are_closed():
    assert parse_hours(BY_ID["gp-ramen"]["regularOpeningHours"]) == {"default": ["17:30", "21:30"], "mon": None}


def test_closing_after_midnight():
    assert parse_hours(BY_ID["gp-bar"]["regularOpeningHours"]) == {"default": ["19:00", "26:00"]}


def test_open_24_hours():
    assert parse_hours(BY_ID["gp-24h"]["regularOpeningHours"]) == {"default": ["00:00", "24:00"]}


@pytest.mark.parametrize(
    "opening",
    [
        None,
        {},
        {"periods": []},
        {"periods": [{"open": {"day": 1, "hour": 10}}]},
        {"periods": [{"open": {"day": 1, "hour": 10}, "close": {"day": 1, "hour": 9}}]},
        {"periods": [{"open": {"day": 1, "hour": 10}, "close": {"day": 1, "hour": 20}}, {"open": {"day": 2}}]},
    ],
    ids=["none", "empty", "no_periods", "not_24h", "closes_before_opening", "missing_close"],
)
def test_unusable_hours(opening):
    assert parse_hours(opening) is None


def test_price_from_range_level_or_estimate():
    assert place_price(BY_ID["gp-ramen"], "restaurant", "日式") == (300, False)
    assert place_price(BY_ID["gp-24h"], "restaurant", "美式") == (150, False)
    assert place_price(BY_ID["gp-bistro"], "restaurant", "義式／西式") == (500, True)
    assert place_price(BY_ID["gp-bar"], "bar", "酒館") == (700, True)
    usd = {"priceRange": {"startPrice": {"currencyCode": "USD", "units": "20"}}}
    assert place_price(usd, "restaurant", None) == (500, True)


def test_records_reuse_the_osm_wording():
    records = {r.external_id: r for p in PLACES if (r := to_record(p, "台北市/信義"))}
    assert set(records) == {"gp-ramen", "gp-bistro", "gp-bar", "gp-24h", "gp-cafe"}  # 歇業、沒有店名的略過

    ramen = records["gp-ramen"]
    assert ramen.description == "日式餐廳，供應拉麵。Google 評分 4.3（1234 則評論）。"
    assert ramen.price_per_person == 300 and ramen.estimated == []
    assert ramen.source_url == "https://maps.google.com/?cid=1001"
    assert ramen.reviews == ["湯頭濃郁，出餐很快"]

    bistro = records["gp-bistro"]
    assert bistro.description == "義式／西式餐廳，有素食選項，可訂位。燈光昏黃、氣氛浪漫的義式餐館。"
    assert bistro.reservable and bistro.estimated == ["price"]

    assert records["gp-bar"].description.startswith("酒館")
    assert records["gp-24h"].description.startswith("美式餐廳，供應漢堡")
    cafe = records["gp-cafe"]
    assert cafe.hours == {"default": ["10:00", "19:00"]} and cafe.estimated == ["hours", "price"]


# ---------- 呼叫 API ----------


def test_search_sends_key_field_mask_and_circle():
    requests = []
    with fake_client(sample_handler(requests)) as client:
        assert len(client.search_nearby(Point(25.041, 121.5655), 865)) == len(PLACES)
    req = requests[0]
    assert req.method == "POST" and str(req.url) == google_places.SEARCH_URL
    assert req.headers["X-Goog-Api-Key"] == "test-key"
    mask = req.headers["X-Goog-FieldMask"].split(",")
    assert {"places.id", "places.regularOpeningHours", "places.priceRange", "places.reviews"} <= set(mask)
    body = json.loads(req.content)
    assert body["locationRestriction"]["circle"] == {"center": {"latitude": 25.041, "longitude": 121.5655}, "radius": 865.0}
    assert body["maxResultCount"] == 20 and body["languageCode"] == "zh-TW"
    assert "restaurant" in body["includedTypes"]


def test_without_atmosphere_only_enterprise_fields_are_requested():
    with PlacesClient("test-key", atmosphere=False) as client:
        mask = set(client.field_mask.split(","))
    assert "places.regularOpeningHours" in mask and "places.priceRange" in mask
    assert not mask & {f"places.{f}" for f in google_places.ATMOSPHERE_FIELDS}


def test_no_places_nearby():
    with fake_client(lambda request: httpx.Response(200, json={})) as client:
        assert client.search_nearby(Point(25.0, 121.5), 500) == []


@pytest.mark.parametrize(
    "response, message",
    [
        (
            httpx.Response(403, json={"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}),
            "金鑰沒有開放 Places API (New)，或帳單帳戶未啟用，HTTP 403 PERMISSION_DENIED",
        ),
        (httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}), "超過 Google 的配額，HTTP 429"),
        (httpx.Response(503, text="busy"), "Google 伺服器錯誤，HTTP 503"),
        (httpx.Response(200, text="<html>"), "不是 JSON"),
        (httpx.Response(200, json=[]), "格式不對"),
    ],
    ids=["forbidden", "quota", "server_error", "not_json", "not_object"],
)
def test_errors_become_places_error(response, message):
    with fake_client(lambda request: response) as client, pytest.raises(PlacesError, match=re.escape(message)):
        client.search_nearby(Point(25.0, 121.5), 500)


def test_connection_error():
    def down(request):
        raise httpx.ConnectError("down")

    with fake_client(down) as client, pytest.raises(PlacesError, match="連不上 Google"):
        client.search_nearby(Point(25.0, 121.5), 500)


# ---------- 每日上限 ----------


def test_daily_limit(mem_session, monkeypatch):
    monkeypatch.setattr(settings, "google_places_daily_limit", 3)
    assert google_places.use_quota(mem_session, 2)
    assert not google_places.use_quota(mem_session, 2)  # 2 + 2 超過 3：一次都不記
    assert google_places.use_quota(mem_session, 1)
    assert not google_places.use_quota(mem_session, 1)
    row = mem_session.scalar(select(ApiUsage))
    assert (row.api, row.calls) == ("google_places", 3)


def test_a_new_day_starts_from_zero(mem_session, monkeypatch):
    monkeypatch.setattr(settings, "google_places_daily_limit", 1)
    monkeypatch.setattr(google_places, "today_tw", lambda: dt.date(2026, 10, 16))
    assert google_places.use_quota(mem_session, 1)
    assert not google_places.use_quota(mem_session, 1)
    monkeypatch.setattr(google_places, "today_tw", lambda: dt.date(2026, 10, 17))
    assert google_places.use_quota(mem_session, 1)


def test_area_queries_each_station_once_and_merges(mem_session, monkeypatch):
    monkeypatch.setattr(settings, "google_places_daily_limit", 10)
    requests = []
    with fake_client(sample_handler(requests)) as client:
        records = google_places.area_restaurants(mem_session, XINYI, client)
    assert len(requests) == 2  # 兩個車站各查一次
    ids = [r.external_id for r in records]
    assert len(ids) == len(set(ids)) == 5  # 兩次回傳同樣的店，合併去重
    assert ids[-1] == "gp-cafe"  # 營業時間是估計的排最後
    assert mem_session.scalar(select(ApiUsage.calls)) == 2


def test_not_enough_calls_left_queries_nothing(mem_session, monkeypatch):
    monkeypatch.setattr(settings, "google_places_daily_limit", 1)
    requests = []
    # 信義有兩個車站，只剩一次時一次都不查，免得只查到一部分的車站
    with fake_client(sample_handler(requests)) as client, pytest.raises(PlacesError, match="上限"):
        google_places.area_restaurants(mem_session, XINYI, client)
    assert requests == []


# ---------- 排程與 MCP 工具 ----------


@pytest.fixture
def google(monkeypatch):
    """開啟 Google 模式。預設回傳信義的樣本；把 google["response"] 換掉就回傳別的。"""
    calls = {"requests": [], "response": None}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["requests"].append(request)
        return calls["response"] or httpx.Response(200, json={"places": PLACES})

    monkeypatch.setattr(settings, "google_places_api_key", "test-key")
    monkeypatch.setattr(settings, "google_places_daily_limit", 1000)
    monkeypatch.setattr(google_places, "make_client", lambda: fake_client(handle))
    return calls


def _plan(session, saturday):
    return services.plan_date(session, PlanRequest(date=saturday, origin="板橋", home_by="23:30"))


def test_plan_uses_google_restaurants_without_storing_them(session, saturday, google):
    res = _plan(session, saturday)
    assert res["restaurant_source"] == {"source": "google", "note": ""}
    assert len(google["requests"]) == 2  # 信義兩個車站
    assert res["plans"], res["rejections"]
    for p in res["plans"]:
        r = p["restaurant"]
        assert r["name"] in OPEN_NAMES and r["url"].startswith("https://maps.google.com/")
        assert r["id"].startswith("gp-")
    assert not session.scalar(select(func.count()).select_from(Restaurant).where(Restaurant.external_id.like("gp-%")))
    # Jev 的判斷快取用 place_id 當對象
    assert session.scalar(select(JevJudgment).where(JevJudgment.subject_key == "restaurant:google:gp-bistro"))


def test_google_failure_falls_back_to_the_database(session, saturday, google):
    google["response"] = httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})
    res = _plan(session, saturday)
    assert res["restaurant_source"]["source"] == "osm"
    assert res["restaurant_source"]["note"].startswith("Google 地圖查詢失敗（")
    assert "HTTP 403" in res["restaurant_source"]["note"]
    assert res["plans"], res["rejections"]  # 用資料庫裡的餐廳照樣排得出來
    assert all(isinstance(p["restaurant"]["id"], int) for p in res["plans"])


def test_over_the_daily_limit_falls_back_without_calling(session, saturday, google, monkeypatch):
    monkeypatch.setattr(settings, "google_places_daily_limit", 0)
    res = _plan(session, saturday)
    assert google["requests"] == []
    assert res["restaurant_source"]["source"] == "osm" and "上限" in res["restaurant_source"]["note"]


def test_without_a_key_google_is_never_called(session, saturday, monkeypatch):
    monkeypatch.setattr(google_places, "make_client", lambda: pytest.fail("沒有金鑰時不該呼叫 Google"))
    assert _plan(session, saturday)["restaurant_source"] == {"source": "osm", "note": ""}


def test_find_restaurants_searches_around_the_cinema(session, google):
    cinema = session.scalar(select(Cinema).where(Cinema.external_id == "TPE-XY-C1"))
    items = services.find_restaurants(session, cinema.id, max_walk_min=10)
    assert len(google["requests"]) == 1
    circle = json.loads(google["requests"][0].content)["locationRestriction"]["circle"]
    assert circle == {"center": {"latitude": cinema.lat, "longitude": cinema.lng}, "radius": 577.0}  # 步行 10 分鐘
    assert items and {i["source"] for i in items} == {"google"}
    assert all(i["walk_min"] <= 10 and i["name"] in OPEN_NAMES for i in items)


def test_meta_reports_where_restaurants_come_from(client, monkeypatch):
    assert client.get("/api/meta").json()["restaurants"] == {"source": "osm"}
    monkeypatch.setattr(settings, "google_places_api_key", "test-key")
    assert client.get("/api/meta").json()["restaurants"] == {"source": "google"}

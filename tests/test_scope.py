"""開放範圍（ACTIVE_CITIES）：範圍外的縣市不列在選單、不能排、不更新，但資料留在資料庫。"""
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app import scope, services
from app.collectors import get_collector, load_coverage
from app.config import Settings, settings
from app.models import Cinema
from app.schemas import PlanRequest

TAOYUAN_AREAS = {"桃園市/桃園區", "桃園市/青埔", "桃園市/中壢", "桃園市/八德"}
TAOYUAN_ORIGINS = {"tyn-taoyuan", "tyn-zhongli", "tyn-hsr", "tyn-bade"}
CLOSED = "^目前只開放桃園市$"


@pytest.fixture
def taoyuan_only(monkeypatch):
    monkeypatch.setattr(settings, "active_cities", "桃園市")


def test_empty_setting_opens_everything(monkeypatch):
    monkeypatch.setattr(settings, "active_cities", "")
    assert scope.active_cities() == ()
    assert scope.is_open("台北市")


def test_cities_follow_the_island_order(monkeypatch):
    monkeypatch.setattr(settings, "active_cities", " 桃園市 , 台北市 ")
    assert scope.active_cities() == ("台北市", "桃園市")
    with pytest.raises(ValueError, match="^目前只開放台北市、桃園市$"):
        scope.require_open("台中市")


def test_unknown_city_is_rejected_at_startup():
    with pytest.raises(ValidationError, match="桃員市"):
        Settings(active_cities="桃員市")
    assert Settings(active_cities="桃園市").active_cities == "桃園市"


def test_menu_lists_only_taoyuan(session, taoyuan_only):
    cities = services.list_cities(session)
    assert [c["name"] for c in cities] == ["桃園市"]
    # 測試資料只有青埔有虛構場次，所以只有青埔會出現在選單（真實資料四個商圈都有）
    areas = {a["id"] for a in cities[0]["areas"]}
    assert "桃園市/青埔" in areas and areas <= TAOYUAN_AREAS
    assert {o["id"] for o in cities[0]["origins"]} == TAOYUAN_ORIGINS


def test_taoyuan_plan_still_works(session, saturday, taoyuan_only):
    res = services.plan_date(session, PlanRequest(date=saturday, origin="中壢", area="桃園市/青埔", home_by="23:30"))
    assert res["plans"], res["rejections"]


def test_closed_area_is_rejected(session, saturday, taoyuan_only):
    with pytest.raises(ValueError, match=CLOSED):
        services.plan_date(session, PlanRequest(date=saturday, origin="中壢", area="台北市/信義"))


def test_closed_origin_is_rejected(session, saturday, taoyuan_only):
    with pytest.raises(ValueError, match=CLOSED):
        services.plan_date(session, PlanRequest(date=saturday, origin="台北車站", area="桃園市/中壢"))
    with pytest.raises(ValueError, match=CLOSED):
        services.check_route(session, "台北車站", "桃園市/中壢")


def test_closed_area_lookups_are_rejected(session, saturday, taoyuan_only):
    with pytest.raises(ValueError, match=CLOSED):
        services.search_showtimes(session, saturday, "台北市/信義")
    xinyi = session.scalars(select(Cinema).where(Cinema.area_id == "台北市/信義")).first()
    with pytest.raises(ValueError, match=CLOSED):
        services.find_restaurants(session, xinyi.id)


def test_showtimes_api_reports_closed_area(client, saturday, taoyuan_only):
    resp = client.get("/api/showtimes", params={"date": saturday.isoformat(), "area": "台北市/信義"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "目前只開放桃園市"


def test_scrapers_only_cover_open_cities(session, taoyuan_only):
    for name in ("atmovies", "osm"):
        assert get_collector(name, session).coverage.cities() == {"桃園市"}
    # 直接呼叫 load_coverage（例如路網檢查）時不受設定影響
    assert len(load_coverage(session).cities()) > 1

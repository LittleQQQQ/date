"""開放範圍（ACTIVE_CITIES）：範圍外的縣市不列在選單、不能排、不更新，但資料留在資料庫。"""
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app import scope, services
from app.collectors import get_collector, load_coverage
from app.config import Settings, settings
from app.models import Area, Cinema, Movie, Restaurant, Showtime
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


def _count(session, model, city=None):
    query = select(func.count()).select_from(model)
    if city:
        query = query.join(Area, model.area_id == Area.id).where(Area.city == city)
    return session.scalar(query)


def _titles(session, area_id=None):
    query = select(Movie.title).join(Showtime, Showtime.movie_id == Movie.id).join(Cinema, Showtime.cinema_id == Cinema.id)
    if area_id:
        query = query.where(Cinema.area_id == area_id)
    return sorted(set(session.scalars(query)))


def test_meta_counts_only_cover_open_cities(session, taoyuan_only):
    counts = services.meta(session)["counts"]
    assert counts == {"cinemas": _count(session, Cinema, "桃園市"), "restaurants": _count(session, Restaurant, "桃園市")}
    assert 0 < counts["cinemas"] < _count(session, Cinema)
    assert 0 < counts["restaurants"] < _count(session, Restaurant)


def test_meta_movies_only_cover_open_cities(session, taoyuan_only):
    # 測試資料裡青埔是桃園唯一有影城的商圈；其他縣市另外有青埔沒放的片
    titles = services.meta(session)["movies"]
    assert titles == _titles(session, "桃園市/青埔")
    assert set(_titles(session)) - set(titles)


def test_showtime_notice_only_counts_open_cities(session, monkeypatch):
    # 基隆市有商圈但測試資料沒有它的影城與場次
    for cities, expected in (("桃園市", True), ("基隆市", False), ("", True)):
        monkeypatch.setattr(settings, "active_cities", cities)
        assert services.data_status(session)["has_showtimes"] is expected, cities


def test_unscoped_meta_is_unchanged(session, monkeypatch):
    monkeypatch.setattr(settings, "active_cities", "")
    meta = services.meta(session)
    assert meta["movies"] == sorted(set(session.scalars(select(Movie.title))))
    assert meta["counts"] == {"cinemas": _count(session, Cinema), "restaurants": _count(session, Restaurant)}


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


def test_chat_without_an_area_stays_in_scope(client, taoyuan_only):
    """以前沒說商圈會用預設的台北市/信義，被開放範圍擋下來（「目前只開放桃園市」）。"""
    resp = client.post("/api/chat", json={"message": "明天從桃園出發看電影"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["request"]["origin"] == "tyn-taoyuan"
    assert data["request"]["area"] == "桃園市/青埔"  # 測試資料的桃園區沒有場次，改挑同縣市有場次的商圈
    assert data["parse"]["sources"]["area"] == "預設"

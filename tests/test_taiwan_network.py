"""全台商圈與交通路網（data/fixture 的 base 資料）的檢查。

影城座標取自開眼電影網地圖頁（2026-10-12）；手寫的車站座標打錯時，影城會分不到預期的商圈。
"""
import datetime as dt

import pytest
from sqlalchemy import delete, select

from app import services
from app.collectors.coverage import load_coverage
from app.collectors.fixture import BaseCollector
from app.ingest import upsert_batch
from app.models import Area, Station, TransitLink
from app.planner import station_name
from app.schemas import PlanRequest
from app.timeutil import to_min

DAY = dt.date(2026, 10, 12)

# （影城, 緯度, 經度, 預期商圈；None 表示這次不涵蓋）
CINEMAS = [
    ("高雄大遠百威秀影城", 22.613072, 120.30395, "高雄市/三多"),
    ("喜樂時代高雄總圖店", 22.6091507, 120.302324, "高雄市/三多"),
    ("大立in89豪華影城", 22.621851, 120.29827, "高雄市/三多"),
    ("基隆秀泰影城", 25.130757, 121.74469, "基隆市/基隆車站"),
    ("新竹大遠百威秀影城", 24.801696, 120.965874, "新竹市/東區"),
    ("新竹巨城威秀影城", 24.809639, 120.973868, "新竹市/東區"),
    ("百老匯影城竹北店", 24.8188713, 121.027148, "新竹縣/竹北"),
    ("頭份尚順威秀影城", 24.693709, 120.896761, "苗栗縣/頭份"),
    ("新月豪華影城", 24.754852, 121.750711, "宜蘭縣/宜蘭市"),
    ("花蓮秀泰影城", 23.990824, 121.605383, "花蓮縣/花蓮車站"),
    ("台東秀泰影城", 22.752125, 121.148693, "台東縣/台東市"),
    ("員林影城", 23.954479, 120.573115, "彰化縣/員林"),
    ("環球中華影城", 23.705753, 120.535669, "雲林縣/斗六"),
    ("南投戲院", 23.907963, 120.687916, "南投縣/南投市"),
    ("嘉年華戲院", 23.478163, 120.441261, "嘉義市/嘉義車站"),
    ("中影屏東影城", 22.670618, 120.491952, "屏東縣/屏東市"),
    ("嘉義秀泰影城", 23.494348, 120.444086, None),
    ("雲林北港秀泰", 23.569749, 120.299199, None),
    ("in89澎湖影城", 23.568284, 119.573911, "澎湖縣/馬公"),
    ("金獅影城", 24.446036, 118.369248, None),  # 金門暫時排除：OSM 在影城附近沒有餐廳資料
]

# 本島商圈：都要能從台北車站去、也回得來
MAIN_ISLAND_AREAS = [
    "台北市/信義",
    "台北市/西門町",
    "新北市/板橋",
    "桃園市/青埔",
    "台中市/七期",
    "台南市/中西區",
    "高雄市/三多",
    "基隆市/基隆車站",
    "新竹市/東區",
    "新竹縣/竹北",
    "苗栗縣/頭份",
    "宜蘭縣/宜蘭市",
    "花蓮縣/花蓮車站",
    "台東縣/台東市",
    "彰化縣/員林",
    "雲林縣/斗六",
    "南投縣/南投市",
    "嘉義市/嘉義車站",
    "屏東縣/屏東市",
]


@pytest.fixture
def base(mem_session):
    upsert_batch(mem_session, "base", BaseCollector().collect(DAY, 1))
    mem_session.commit()
    return mem_session


def _area_stations(session, area_id):
    return [s.id for s in session.scalars(select(Station).where(Station.area_id == area_id))]


def _reachable_both_ways(graph, origin, stations):
    return any(graph.reachable(origin, s) for s in stations) and any(graph.reachable(s, origin) for s in stations)


@pytest.mark.parametrize("name, lat, lng, expected", CINEMAS)
def test_cinema_lands_in_expected_area(base, name, lat, lng, expected):
    assert load_coverage(base).area_for(lat, lng) == expected, name


@pytest.mark.parametrize("area_id", MAIN_ISLAND_AREAS)
def test_main_island_area_connects_to_taipei(base, area_id):
    stations = _area_stations(base, area_id)
    assert stations, f"{area_id} 沒有車站"
    assert _reachable_both_ways(services.load_graph(base), "tpe-main", stations), area_id


def test_dome_is_only_a_transfer_station_now(base):
    assert base.get(Area, "高雄市/巨蛋") is None
    dome = base.get(Station, "khh-dome")
    assert dome.area_id is None and dome.is_origin


# 離島商圈：只能從島上的出發站去（金門暫時排除）
ISLAND_AREAS = {
    "澎湖縣/馬公": ["pen-magong"],
}


@pytest.mark.parametrize("area_id, island_origins", ISLAND_AREAS.items())
def test_island_area_only_reachable_from_the_island(base, area_id, island_origins):
    stations = _area_stations(base, area_id)
    assert stations, f"{area_id} 沒有車站"
    graph = services.load_graph(base)
    assert not any(graph.reachable("tpe-main", s) for s in stations)
    for origin in island_origins:
        assert _reachable_both_ways(graph, origin, stations), (area_id, origin)


def test_nineteen_cities_have_areas(base):
    areas = base.scalars(select(Area)).all()
    assert len(areas) == 20
    cities = {a.city for a in areas}
    assert len(cities) == 19
    assert not {"嘉義縣", "連江縣", "金門縣"} & cities


def test_kinmen_is_excluded_for_now(base):
    """金門的影城附近 OSM 幾乎沒有標記餐廳，排不出方案，先不列；車站也不留。"""
    assert not base.scalars(select(Station).where(Station.city == "金門縣")).all()


def test_every_area_has_a_station(base):
    for area in base.scalars(select(Area)):
        assert _area_stations(base, area.id), area.id


def test_check_route_reaches_new_areas(base):
    assert services.check_route(base, "台北車站", "基隆市/基隆車站", "18:00")
    assert services.check_route(base, "馬公總站", "澎湖縣/馬公", "18:00")


def test_area_lists_cover_every_area(base):
    """新增商圈卻忘了加進上面的清單，路網檢查就會漏掉它。"""
    all_areas = {a.id for a in base.scalars(select(Area))}
    assert set(MAIN_ISLAND_AREAS) | set(ISLAND_AREAS) == all_areas


def test_every_link_endpoint_exists(base):
    """SQLite 不檢查外鍵，手寫路段的車站代碼打錯時不會報錯，只是路網少一段。"""
    ids = {s.id for s in base.scalars(select(Station))}
    for link in base.scalars(select(TransitLink)):
        assert link.from_station_id in ids, link.line
        assert link.to_station_id in ids, link.line


def test_links_run_forward(base):
    for link in base.scalars(select(TransitLink)):
        assert to_min(link.first_train) < to_min(link.last_train), (link.from_station_id, link.to_station_id, link.line)


@pytest.mark.parametrize("area_id", MAIN_ISLAND_AREAS)
def test_main_island_area_timed_round_trip(base, area_id):
    """考慮首末班車：早上 10 點從台北車站出發到得了，午夜前也有車回台北車站。"""
    graph = services.load_graph(base)
    stations = _area_stations(base, area_id)
    assert any(
        graph.earliest_arrival("tpe-main", to_min("10:00"), s) is not None
        and graph.latest_departure(s, "tpe-main", to_min("24:00")) is not None
        for s in stations
    ), area_id


def test_station_names_never_render_a_doubled_zhan(base):
    """畫面一律在站名後面補「站」（已經有就不補）；站名本身不能讓它變成「站站」或「站（…）站」。"""
    for station in base.scalars(select(Station)):
        shown = station_name(station.name)
        assert "站站" not in shown and "站（" not in shown, (station.id, shown)


def test_island_origin_to_main_island_has_a_clear_message(base):
    req = PlanRequest(date="2026-10-13", origin="tpe-main", area="澎湖縣/馬公")
    with pytest.raises(ValueError, match="^澎湖只能從島上出發，請改選島上的出發站（例如馬公總站）$"):
        services.plan_date(base, req)
    req = PlanRequest(date="2026-10-13", origin="pen-magong", area="台北市/信義")
    with pytest.raises(ValueError, match="^澎湖只能從島上出發"):
        services.plan_date(base, req)


def test_no_route_message_does_not_double_zhan(base):
    """該商圈真的沒有交通資料時（這裡把基隆的路段刪掉），訊息裡的站名要正常。"""
    base.execute(delete(TransitLink).where((TransitLink.from_station_id == "kel-main") | (TransitLink.to_station_id == "kel-main")))
    req = PlanRequest(date="2026-10-13", origin="tpe-main", area="基隆市/基隆車站")
    with pytest.raises(ValueError) as err:
        services.plan_date(base, req)
    assert "從台北市台北車站到基隆市基隆車站" in str(err.value)


@pytest.mark.parametrize("raw, station_id", [("基隆車站", "kel-main"), ("花蓮車站", "hua-main"), ("台北車站", "tpe-main"), ("南投（統聯）站", "nan-nantou")])
def test_resolve_station_accepts_x_chezhan(base, raw, station_id):
    assert services.resolve_station(base, raw).id == station_id


def test_bus_lines_start_with_the_mode(base):
    """路線名稱沒有以「公車」開頭時，顯示會變成「公車國道客運…」。"""
    for link in base.scalars(select(TransitLink).where(TransitLink.mode == "公車")):
        assert link.line.startswith("公車"), link.line


@pytest.mark.parametrize(
    "src, dst, line",
    [
        ("tnn-main", "khh-zuoying", "台鐵縱貫線"),
        ("txg-hsr", "chw-yuanlin", "台鐵縱貫線"),
        ("tpe-main", "hua-main", "台鐵東部幹線（台北—花蓮直達）"),
    ],
)
def test_cross_county_rail_follows_the_real_lines(base, src, dst, line):
    """台南接左營（再續縱貫線到高雄）、新烏日直達員林、台北花蓮有直達車，不必繞路。"""
    links = base.scalars(select(TransitLink).where(TransitLink.from_station_id == src, TransitLink.to_station_id == dst))
    assert [link.line for link in links] == [line]


def _plan_context(session, origin_id, area_id):
    """只有交通的排程情境（沒有場次和餐廳），用來直接測找走法。"""
    from app.planner import PlanContext, Place

    def place(station):
        return Place(station.id, station.name, station.lat, station.lng)

    stations = session.scalars(select(Station).where(Station.area_id == area_id)).all()
    return PlanContext(
        origin=place(session.get(Station, origin_id)), area_stations=[place(s) for s in stations],
        graph=services.load_graph(session), showtimes=[], restaurants=[], weekend=False, threshold=0.6, ask=None,
    )


def test_tainan_can_be_reached_without_high_speed_rail(base):
    from app.planner import _out_options

    ctx = _plan_context(base, "tpe-main", "台南市/中西區")
    tainan = next(p for p in ctx.area_stations if p.id == "tnn-main")
    early = PlanRequest(date="2026-10-13", depart_after="08:00")
    options = _out_options(ctx, early, tainan, to_min("18:00"), every=True)
    labels = [o.label for o in options]
    assert any("高鐵" in label for label in labels) and any("高鐵" not in label for label in labels), labels
    departs = [o.route.depart for o in options]
    assert departs == sorted(departs, reverse=True)  # 由快到慢：越晚出門越前面
    fares = [o.fare for o in options]
    assert fares == sorted(fares, reverse=True)  # 慢的一定比較便宜，不然會被刪掉
    # 下午兩點才能出門，只搭台鐵趕不上，剩下有搭高鐵的走法
    late = PlanRequest(date="2026-10-13", depart_after="14:00")
    assert all("高鐵" in o.label for o in _out_options(ctx, late, tainan, to_min("18:00"), every=True))


def test_metro_trip_has_a_single_option(base):
    from app.planner import _home_options, _out_options

    ctx = _plan_context(base, "ntp-banqiao", "台北市/信義")
    cityhall = next(p for p in ctx.area_stations if p.id == "tpe-cityhall")
    req = PlanRequest(date="2026-10-13")
    go = _out_options(ctx, req, cityhall, to_min("18:00"), every=True)
    back = _home_options(ctx, req, cityhall, to_min("21:00"), every=True)
    assert [(o.label, o.fare) for o in go] == [("捷運板南線", 45)]
    assert [o.label for o in back] == ["捷運板南線"]

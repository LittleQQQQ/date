"""地點之間怎麼走（規格書 v0.2 7.1 第 3 步、5.3 路線快取）。"""
import pytest

from app.redis_utils.cache import get_json, route_key
from app.routing import Place, Router, StationPlace
from app.timeutil import to_min
from app.transit import Edge, Node, TransitGraph

pytestmark = pytest.mark.usefixtures("redis_client")

# 甲站、乙站相距約 5.5 公里；甲地、乙地各在車站旁邊（約 110 公尺）
STA_A = StationPlace("sta-a", "甲站", 24.9500, 121.2200)
STA_B = StationPlace("sta-b", "乙站", 25.0000, 121.2200)
HOME = Place("loc1", "甲地", 24.9510, 121.2200)
MALL = Place("cin1", "乙地影城", 25.0010, 121.2200)
NEXT_DOOR = Place("res1", "甲地隔壁", 24.9550, 121.2200)  # 甲地北邊約 450 公尺


def router(version=1, fare=18, ride=20, use_cache=True):
    first, last = to_min("06:00"), to_min("22:00")
    graph = TransitGraph(
        [Node("sta-a", "甲站"), Node("sta-b", "乙站")],
        [Edge("sta-a", "sta-b", "公車", "公車（170）", ride, 15, first, last, fare),
         Edge("sta-b", "sta-a", "公車", "公車（170）", ride, 15, first, last, fare)],
    )
    return Router(graph, [STA_A, STA_B], network_version=version, use_cache=use_cache)


def test_short_distances_are_walked():
    trip = router().depart_after(HOME, NEXT_DOOR, to_min("18:00"))
    assert trip.is_walk and trip.depart == to_min("18:00")
    assert trip.walk_min == trip.arrive - trip.depart == 8  # 450 公尺 × 1.3 ÷ 每分鐘 75 公尺
    assert trip.summary == "步行約 8 分鐘" and trip.fare == 0


def test_longer_trips_ride_and_round_the_departure_up():
    trip = router().depart_after(HOME, MALL, to_min("18:07"))
    assert trip.depart == to_min("18:15")  # 往後取整到 15 分鐘，不會錯過車
    assert trip.summary == "公車（170）" and trip.fare == 18 and not trip.fare_estimated
    [ride] = trip.rides
    assert ride.board >= trip.depart + 2  # 先走到車站
    assert trip.arrive == ride.alight + 2  # 下車再走到影城


def test_routes_are_cached_by_network_version(redis_client):
    first = router(version=7).depart_after(HOME, MALL, to_min("18:07"))
    key = route_key(7, "loc1", "cin1", to_min("18:15"))
    assert get_json(key)["rides"][0]["line"] == "公車（170）"
    cached = router(version=7, fare=99).depart_after(HOME, MALL, to_min("18:10"))  # 同一個時段：直接用快取
    assert cached.fare == 18 and cached.arrive == first.arrive
    fresh = router(version=8, fare=99).depart_after(HOME, MALL, to_min("18:10"))  # 路網更新了：重算
    assert fresh.fare == 99


def test_cache_never_stores_place_names(redis_client):
    """Google 餐廳的店名不能存進 Redis；同一個地點補完店家資料後改名，路線也要顯示新名稱。"""
    r = router()
    placeholder = Place("res9", "（Google 地圖上的餐廳）", MALL.lat, MALL.lng)
    r.depart_after(HOME, placeholder, to_min("18:07"))
    assert "from_name" not in get_json(route_key(1, "loc1", "res9", to_min("18:15")))
    real = Place("res9", "麵屋一燈", MALL.lat, MALL.lng)
    assert r.depart_after(HOME, real, to_min("18:07")).to_name == "麵屋一燈"  # 同一個 Router 的記憶
    assert router().depart_after(HOME, real, to_min("18:07")).to_name == "麵屋一燈"  # Redis 快取
    assert r.arrive_by(HOME, placeholder, to_min("19:00"), 0).to_name == "（Google 地圖上的餐廳）"
    assert r.arrive_by(HOME, real, to_min("19:00"), 0).to_name == "麵屋一燈"


def test_arrive_by_leaves_as_late_as_possible():
    r = router()
    trip = r.arrive_by(HOME, MALL, to_min("19:00"), not_before=to_min("17:00"))
    assert trip.arrive <= to_min("19:00") and trip.depart >= to_min("17:00")
    assert r.arrive_by(HOME, MALL, to_min("19:00"), not_before=to_min("18:50")) is None  # 來不及
    walk = r.arrive_by(HOME, NEXT_DOOR, to_min("19:00"), not_before=to_min("17:00"))
    assert walk.is_walk and (walk.depart, walk.arrive) == (to_min("18:52"), to_min("19:00"))


def test_places_without_a_station_nearby_are_unreachable():
    far = Place("res9", "山上", 25.2000, 121.5000)
    assert router(use_cache=False).depart_after(HOME, far, to_min("18:00")) is None

"""全台商圈與交通路網（data/fixture 的 base 資料）的檢查。

影城座標取自開眼電影網地圖頁（2026-10-12）；手寫的車站座標打錯時，影城會分不到預期的商圈。
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app import services
from app.collectors.coverage import load_coverage
from app.collectors.fixture import BaseCollector
from app.ingest import upsert_batch
from app.models import Area, Station

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
]

# 本島商圈：都要能從台北車站去、也回得來
MAIN_ISLAND_AREAS = [
    "高雄市/三多",
    "基隆市/基隆車站",
    "新竹市/東區",
    "新竹縣/竹北",
    "苗栗縣/頭份",
    "宜蘭縣/宜蘭市",
    "花蓮縣/花蓮車站",
    "台東縣/台東市",
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

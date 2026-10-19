"""涵蓋範圍：爬蟲只留「走得到 4 個區域的車站」的影城和餐廳。

離某個有區域的車站直線 WALK_RADIUS_M 公尺內就算涵蓋（規格書 7.1 用同一個門檻判斷走路）。
這裡只決定要不要抓、抓哪裡；影城和餐廳屬於哪個區域由 Jev 判斷（規格書 4.3）。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..geo import distance_m
from ..models import Area, Station, lat_of, lng_of

WALK_RADIUS_M = 800


@dataclass(frozen=True)
class CoveredStation:
    code: str
    name: str
    area_code: str
    lat: float
    lng: float


@dataclass
class Coverage:
    stations: list[CoveredStation] = field(default_factory=list)

    def nearest(self, lat: float, lng: float) -> tuple[CoveredStation, float] | None:
        """走得到的車站裡最近的一個，以及直線距離（公尺）；都太遠回傳 None。"""
        point = _Point(lat, lng)
        best = min(((s, distance_m(s, point)) for s in self.stations), key=lambda x: x[1], default=None)
        return best if best and best[1] <= WALK_RADIUS_M else None

    def by_area(self) -> dict[str, list[CoveredStation]]:
        grouped: dict[str, list[CoveredStation]] = defaultdict(list)
        for s in self.stations:
            grouped[s.area_code].append(s)
        return dict(grouped)


@dataclass(frozen=True)
class _Point:
    lat: float
    lng: float


def load_coverage(session: Session) -> Coverage:
    """有區域的車站（路網骨架裡標了 area 的站）。"""
    rows = session.execute(
        select(Station.code, Station.name, Area.code, lat_of(Station.geom), lng_of(Station.geom))
        .join(Area, Area.id == Station.area_id)
        .order_by(Station.code)
    ).all()
    return Coverage([CoveredStation(code, name, area, lat, lng) for code, name, area, lat, lng in rows])

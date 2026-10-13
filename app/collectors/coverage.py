"""商圈涵蓋範圍：每個商圈有哪些車站與影城，用來決定爬蟲要抓哪裡、抓到的地點屬於哪個商圈。

地點離某個商圈任一車站步行不超過 STATION_WALK_MAX 分鐘，就屬於那個商圈（排程用同一個上限）；
同時離好幾個商圈都夠近時，取走路最近的。
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..geo import walk_minutes
from ..models import Area, Cinema, Station
from ..planner import STATION_WALK_MAX


@dataclass(frozen=True)
class Point:
    lat: float
    lng: float


@dataclass
class AreaCoverage:
    id: str
    city: str
    stations: list[Point]
    cinemas: list[Point] = field(default_factory=list)

    def walk_from_station(self, p: Point) -> int:
        return min(walk_minutes(s, p) for s in self.stations)

    def walk_to_cinemas(self, p: Point) -> int:
        """到最近影城的步行分鐘數；還沒有影城資料時改用最近的車站。"""
        return min(walk_minutes(t, p) for t in (self.cinemas or self.stations))


@dataclass
class Coverage:
    areas: list[AreaCoverage]

    def cities(self) -> set[str]:
        return {a.city for a in self.areas}

    def area_for(self, lat: float, lng: float) -> str | None:
        p = Point(lat, lng)
        best = min(((a.walk_from_station(p), a.id) for a in self.areas if a.stations), default=None)
        return best[1] if best and best[0] <= STATION_WALK_MAX else None

    def get(self, area_id: str) -> AreaCoverage:
        return next(a for a in self.areas if a.id == area_id)


def load_coverage(session: Session, cities: Collection[str] = ()) -> Coverage:
    """cities 有值時只載入這些縣市的商圈（爬蟲用，見 app/scope.py）；空的代表全部。"""
    areas = []
    for area in session.scalars(select(Area).order_by(Area.id)):
        if cities and area.city not in cities:
            continue
        stations = [Point(s.lat, s.lng) for s in session.scalars(select(Station).where(Station.area_id == area.id))]
        if not stations:
            continue
        cinemas = [Point(c.lat, c.lng) for c in session.scalars(select(Cinema).where(Cinema.area_id == area.id))]
        areas.append(AreaCoverage(area.id, area.city, stations, cinemas))
    return Coverage(areas)

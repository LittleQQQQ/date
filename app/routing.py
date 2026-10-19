"""地點之間怎麼走（規格書 v0.2 7.1 第 3 步）：地標、影城、餐廳之間的一段路。

- 直線距離 800 公尺內算走路：距離 × 1.3 ÷ 每分鐘 75 公尺（規格書【預設】）。
- 更遠就搭車：起點走到 800 公尺內的車站，用路網（transit.py）找轉乘，再從車站走到終點。
  站之間的班表、車資來自 TDX（沒有的路段用估計值）。
- 「幾點出發」的查詢放進 Redis 路線快取（規格書 5.3 A2）：出發時間往後取整到 15 分鐘，查「取整後出發」，
  保證使用者不會錯過車；key 帶路網版本，TDX 更新後舊快取自然過期。

時間一律是「規劃那天 00:00 起的分鐘數」，跨午夜可以大於 1440。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .fares import leg_cost
from .geo import distance_m, walk_minutes
from .redis_utils.cache import ROUTE_TTL_S, get_json, route_key, route_slot, set_json
from .transit import Route, TransitGraph

WALK_MAX_M = 800


@dataclass(frozen=True)
class Place:
    key: str  # 快取用：loc12、cin3、res45、sta7
    name: str
    lat: float
    lng: float


@dataclass(frozen=True)
class StationPlace:
    code: str
    name: str
    lat: float
    lng: float


@dataclass
class Ride:
    """搭同一條路線的一段車。"""

    line: str
    from_name: str
    to_name: str
    board: int
    alight: int
    wait: int
    fare: int
    fare_estimated: bool


@dataclass
class Trip:
    """兩個地點之間的一段路：純走路，或走到車站、搭車、走到目的地。"""

    from_name: str
    to_name: str
    depart: int
    arrive: int
    walk_min: int  # 走路合計
    rides: list[Ride] = field(default_factory=list)

    @property
    def is_walk(self) -> bool:
        return not self.rides

    @property
    def fare(self) -> int:
        return sum(r.fare for r in self.rides)

    @property
    def fare_estimated(self) -> bool:
        return any(r.fare_estimated for r in self.rides)

    @property
    def transfers(self) -> int:
        return max(0, len(self.rides) - 1)

    @property
    def longest_wait(self) -> int:
        return max((r.wait for r in self.rides), default=0)

    @property
    def summary(self) -> str:
        """「公車（170、5087） → 台鐵縱貫線（實際班次）」；走路是「步行約 8 分鐘」。"""
        if self.is_walk:
            return f"步行約 {self.walk_min} 分鐘"
        return " → ".join(r.line for r in self.rides)

    def to_cache(self) -> dict:
        return asdict(self)

    @classmethod
    def from_cache(cls, data: dict) -> Trip:
        return cls(**{**data, "rides": [Ride(**r) for r in data["rides"]]})


def _rides(route: Route) -> list[Ride]:
    return [
        Ride(leg.label, leg.from_name, leg.to_name, leg.board, leg.alight, leg.wait,
             leg_cost(leg), leg.fare is None)
        for leg in route.legs
    ]


class Router:
    def __init__(self, graph: TransitGraph, stations: list[StationPlace], network_version: int = 0, use_cache: bool = True):
        self.graph = graph
        self.stations = stations
        self.network_version = network_version
        self.use_cache = use_cache
        self._near: dict[str, list[tuple[StationPlace, int]]] = {}
        self._memo: dict[tuple, Trip | None] = {}

    def nearby(self, place: Place) -> list[tuple[StationPlace, int]]:
        """走得到的車站（800 公尺內）和走路分鐘數，近的排前面。"""
        if place.key not in self._near:
            found = [(s, walk_minutes(place, s)) for s in self.stations if distance_m(place, s) <= WALK_MAX_M]
            self._near[place.key] = sorted(found, key=lambda x: x[1])
        return self._near[place.key]

    def walk(self, a: Place, b: Place) -> int | None:
        return walk_minutes(a, b) if distance_m(a, b) <= WALK_MAX_M else None

    def depart_after(self, a: Place, b: Place, t: int) -> Trip | None:
        """t 之後從 a 出發，最早到 b 的走法；搭車時出發時間會往後取整到 15 分鐘。走不到、沒車回 None。"""
        if (w := self.walk(a, b)) is not None:
            return Trip(a.name, b.name, t, t + w, w)
        slot = route_slot(t)
        memo = ("after", a.key, b.key, slot)
        if memo not in self._memo:
            self._memo[memo] = self._cached(a, b, slot)
        return self._memo[memo]

    def _cached(self, a: Place, b: Place, slot: int) -> Trip | None:
        key = route_key(self.network_version, a.key, b.key, slot)
        if self.use_cache and (data := get_json(key)) is not None:
            return Trip.from_cache(data) if data else None
        trip = self._earliest(a, b, slot)
        if self.use_cache:
            set_json(key, trip.to_cache() if trip else {}, ROUTE_TTL_S)  # 沒車也記下來，免得一直重算
        return trip

    def _earliest(self, a: Place, b: Place, t: int) -> Trip | None:
        best: Trip | None = None
        for sa, wa in self.nearby(a):
            for sb, wb in self.nearby(b):
                if sa.code == sb.code:
                    continue
                route = self.graph.earliest_arrival(sa.code, t + wa, sb.code)
                if route is None or not route.legs:
                    continue
                trip = Trip(a.name, b.name, t, route.arrive + wb, wa + wb, _rides(route))
                if best is None or trip.arrive < best.arrive:
                    best = trip
        return best

    def arrive_by(self, a: Place, b: Place, deadline: int, not_before: int) -> Trip | None:
        """要在 deadline 前到 b，最晚幾點從 a 出發（不能早於 not_before）。走路的話剛好在 deadline 到。"""
        if (w := self.walk(a, b)) is not None:
            return Trip(a.name, b.name, deadline - w, deadline, w) if deadline - w >= not_before else None
        memo = ("by", a.key, b.key, deadline)
        if memo not in self._memo:
            best: Trip | None = None
            for sa, wa in self.nearby(a):
                for sb, wb in self.nearby(b):
                    if sa.code == sb.code:
                        continue
                    route = self.graph.latest_departure(sa.code, sb.code, deadline - wb)
                    if route is None or not route.legs:
                        continue
                    trip = Trip(a.name, b.name, route.depart - wa, route.arrive + wb, wa + wb, _rides(route))
                    if best is None or trip.depart > best.depart:
                        best = trip
            self._memo[memo] = best
        trip = self._memo[memo]
        return trip if trip is not None and trip.depart >= not_before else None


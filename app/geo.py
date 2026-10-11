"""距離與步行時間。沒有路網資料，先用直線距離乘上繞路係數估算。"""
from __future__ import annotations

import math
from typing import Protocol

WALK_M_PER_MIN = 75
DETOUR = 1.3
EARTH_RADIUS_M = 6_371_000


class HasLocation(Protocol):
    lat: float
    lng: float


def distance_m(a: HasLocation, b: HasLocation) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def walk_minutes(a: HasLocation, b: HasLocation) -> int:
    return max(1, math.ceil(distance_m(a, b) * DETOUR / WALK_M_PER_MIN))

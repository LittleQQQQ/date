"""車資（每人單程）。

有 TDX 真實票價的路段（台鐵區間車、公車成人全票）用真實票價；沒有的依交通方式和車程分鐘數估，畫面標「車資為估計」。
連續搭同一條路線算一段票（transit.Leg）；估計的每段四捨五入到 5 元，一趟是各段相加。
"""
from __future__ import annotations

from .transit import Leg, Route

CITY_BUS_MAX_MIN = 30  # 公車車程在這以內算市區公車（一段票），更長算國道客運


def _round5(value: float) -> int:
    return int(value / 5 + 0.5) * 5


def leg_fare(mode: str, ride_min: int) -> int:
    """估計一段車（連續搭同一條路線）的每人車資。"""
    if mode in ("捷運", "輕軌"):
        fare = min(20 + ride_min, 65)
    elif mode == "公車":
        fare = 15 if ride_min <= CITY_BUS_MAX_MIN else 2.3 * ride_min
    elif mode == "台鐵":
        fare = max(2.5 * ride_min, 15)
    elif mode == "高鐵":
        fare = max(13 * ride_min, 40)
    else:
        raise ValueError(f"不認得的交通方式：{mode}")
    return _round5(fare)


def leg_cost(leg: Leg) -> int:
    """這段車的每人車資：有真實票價就用，沒有才估。"""
    return leg.fare if leg.fare is not None else leg_fare(leg.mode, leg.ride_min)


def route_fare(route: Route) -> int:
    """一趟的每人車資；走路就到是 0。"""
    return sum(leg_cost(leg) for leg in route.legs)


def fare_estimated(route: Route) -> bool:
    """這趟有沒有哪一段的車資是估計的。"""
    return any(leg.fare is None for leg in route.legs)

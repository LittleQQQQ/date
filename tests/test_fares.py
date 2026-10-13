import pytest

from app.fares import leg_fare, route_fare
from app.timeutil import to_min
from app.transit import Edge, Node, Route, TransitGraph


@pytest.mark.parametrize(
    "mode, ride, fare",
    [
        ("捷運", 3, 25),  # 20 + 3 = 23 → 25
        ("捷運", 24, 45),  # 板橋到市政府
        ("捷運", 80, 65),  # 最多 65 元
        ("輕軌", 10, 30),
        ("公車", 25, 15),  # 市區公車一段票
        ("公車", 60, 140),  # 國道客運：60 × 2.3 = 138 → 140
        ("台鐵", 4, 15),  # 最少 15 元
        ("台鐵", 150, 375),
        ("高鐵", 3, 40),  # 最少 40 元
        ("高鐵", 54, 700),  # 54 × 13 = 702 → 700
    ],
)
def test_leg_fare(mode, ride, fare):
    assert leg_fare(mode, ride) == fare


def test_unknown_mode_is_an_error():
    with pytest.raises(ValueError, match="不認得的交通方式"):
        leg_fare("渡輪", 30)


def test_same_line_is_one_ticket():
    """A→B→C 是同一條捷運，車程合計 10 分鐘，算一段票 30 元，不是兩段 25 + 25。"""
    first, last = to_min("06:00"), to_min("23:00")
    graph = TransitGraph(
        [Node("A", "甲"), Node("B", "乙"), Node("C", "丙")],
        [Edge("A", "B", "捷運", "L1", 5, 10, first, last), Edge("B", "C", "捷運", "L1", 5, 10, first, last)],
    )
    route = graph.earliest_arrival("A", to_min("18:00"), "C")
    assert route_fare(route) == 30


def test_route_fare_adds_up_each_leg():
    first, last = to_min("06:00"), to_min("23:00")
    graph = TransitGraph(
        [Node("A", "台中"), Node("B", "台北"), Node("C", "市政府")],
        [Edge("A", "B", "高鐵", "高鐵", 58, 20, first, last), Edge("B", "C", "捷運", "板南線", 11, 6, first, last)],
    )
    route = graph.earliest_arrival("A", to_min("18:00"), "C")
    assert [leg_fare(leg.mode, leg.ride_min) for leg in route.legs] == [755, 30]
    assert route_fare(route) == 785


def test_walking_costs_nothing():
    assert route_fare(Route(to_min("18:00"), to_min("18:00"), [])) == 0

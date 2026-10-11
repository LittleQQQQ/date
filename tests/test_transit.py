from app.timeutil import to_min
from app.transit import Edge, Node, TransitGraph


def _graph():
    # A —L1— B —L1— C   以及   B —L2— D（在 B 換線要走 4 分鐘）
    nodes = [Node("A", "甲"), Node("B", "乙", transfer_min=4), Node("C", "丙"), Node("D", "丁")]
    first, last = to_min("06:00"), to_min("23:00")
    edges = [
        Edge("A", "B", "捷運", "L1", 5, 10, first, last),
        Edge("B", "C", "捷運", "L1", 5, 10, first, last),
        Edge("B", "D", "公車", "L2", 7, 6, first, last),
    ]
    return TransitGraph(nodes, edges)


def test_same_line_does_not_wait_twice():
    route = _graph().earliest_arrival("A", to_min("18:00"), "C")
    assert len(route.legs) == 1  # A→B→C 同一班車，合併成一段
    assert route.legs[0].board == to_min("18:10")  # 最壞情況等一個班距
    assert route.arrive == to_min("18:20")


def test_transfer_adds_walk_and_wait():
    route = _graph().earliest_arrival("A", to_min("18:00"), "D")
    first, second = route.legs
    assert first.alight == to_min("18:15")
    assert second.transfer_min == 4 and second.ready == to_min("18:19")
    assert second.board == to_min("18:25")
    assert route.arrive == to_min("18:32")


def test_latest_departure_is_really_the_latest():
    graph = _graph()
    deadline = to_min("19:00")
    route = graph.latest_departure("A", "D", deadline)
    assert graph.earliest_arrival("A", route.depart, "D").arrive <= deadline
    assert graph.earliest_arrival("A", route.depart + 1, "D").arrive > deadline


def test_last_and_first_train():
    graph = _graph()
    assert graph.earliest_arrival("A", to_min("23:05"), "C") is None
    early = graph.earliest_arrival("A", to_min("05:00"), "C")
    assert early.legs[0].board == to_min("06:00")
    assert graph.latest_departure("A", "C", to_min("06:05")) is None


def test_same_station_needs_no_ride():
    route = _graph().latest_departure("C", "C", to_min("20:00"))
    assert route.legs == [] and route.depart == to_min("20:00")
    assert not _graph().reachable("D", "A")

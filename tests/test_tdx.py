"""TDX 交通資料：手做的小份 TDX 回應，不連網、不耗用金鑰額度。"""
import datetime as dt
import json

import pytest
from sqlalchemy import func, select

from app.collectors import tdx
from app.collectors.base import ScrapeError
from app.collectors.http import FetchError
from app.ingest import load_network, make_collector, run_collector
from app.models import TransitFare, TransitLink
from app.timeutil import hhmm, to_min

DAY = dt.date(2026, 10, 16)
ALL_DAYS = {d: 1 for d in tdx.WEEKDAYS}
WEEKDAYS_ONLY = ALL_DAYS | {"Saturday": 0, "Sunday": 0}

A = tdx.NetStation("tyn-a", "甲站", 24.9500, 121.2200)
B = tdx.NetStation("tyn-b", "乙站", 24.9600, 121.2200)  # 甲站北邊約 1.1 公里
ZHONGLI = tdx.NetStation("tyn-zhongli", "中壢", 24.9536, 121.2257)
TAOYUAN = tdx.NetStation("tyn-taoyuan", "桃園", 24.9891, 121.3135)


def _stop(stop_id, seq, lat, lng):
    return {"StopID": stop_id, "StopSequence": seq, "StopPosition": {"PositionLat": lat, "PositionLon": lng}}


def _route(name, stops):
    return {"RouteName": {"Zh_tw": name}, "SubRouteUID": f"TAO{name}", "SubRouteID": name, "Direction": 0, "Stops": stops}


STRAIGHT = [_stop("1", 1, 24.9500, 121.2200), _stop("2", 2, 24.9550, 121.2200), _stop("3", 3, 24.9600, 121.2200)]
# 環狀線：從甲站繞到 20 公里外再回到乙站，不算順路
LOOP = [_stop("1", 1, 24.9500, 121.2200), _stop("9", 2, 25.1300, 121.2200), _stop("3", 3, 24.9600, 121.2200)]
ROUTES = [_route("R1", STRAIGHT), _route("R2", STRAIGHT), _route("LOOP", LOOP)]


def _trips(start, end, every, service_day, extra=()):
    times = [hhmm(t) for t in range(to_min(start), to_min(end) + 1, every)] + list(extra)
    return [{"ServiceDay": service_day, "StopTimes": [{"StopSequence": 1, "DepartureTime": t}]} for t in times]


SCHEDULES = [
    # R1 每天 06:00～22:00 每 20 分鐘一班，深夜另有一班孤零零的 23:59
    {"SubRouteUID": "TAOR1", "Direction": 0, "Timetables": _trips("06:00", "22:00", 20, ALL_DAYS, ["23:59"])},
    # R2 只有平日，和 R1 錯開 10 分鐘
    {"SubRouteUID": "TAOR2", "Direction": 0, "Timetables": _trips("06:10", "21:50", 20, WEEKDAYS_ONLY)},
    {"SubRouteUID": "TAOLOOP", "Direction": 0, "Timetables": _trips("06:05", "22:05", 10, ALL_DAYS)},
]


def _od(sub_route, origin, dest, price):
    return {"RouteName": sub_route, "SubRouteID": sub_route, "IsFreeBus": 0, "ODFares": [{
        "Direction": 0, "OriginStop": {"StopID": origin}, "DestinationStop": {"StopID": dest},
        "Fares": [{"TicketType": 1, "FareClass": 1, "Price": price}, {"TicketType": 1, "FareClass": 10, "Price": price // 2}],
    }]}


ROUTE_FARES = [_od("R1", "1", "3", 18), _od("R2", "1", "3", 18)]


def _train(dep):
    arr = hhmm((to_min(dep) + 10) % (24 * 60))
    return {"TrainInfo": {}, "StopTimes": [
        {"StationID": "1080", "StationName": {"Zh_tw": "桃園"}, "StopSequence": 1, "DepartureTime": dep, "ArrivalTime": dep},
        {"StationID": "1100", "StationName": {"Zh_tw": "中壢"}, "StopSequence": 2, "DepartureTime": arr, "ArrivalTime": arr},
    ]}


# 桃園 → 中壢：05:30～23:45 每 15 分鐘一班，車程 10 分鐘；另有一班跨午夜的 00:00（算前一天的末班 24:00）
TIMETABLE = {"TrainTimetables": [_train(hhmm(t)) for t in range(to_min("05:30"), to_min("23:45") + 1, 15)] + [_train("00:00")]}
TRA_FARES = {"ODFares": [
    {"Direction": 1, "TrainType": 6, "Fares": [{"TicketType": 1, "FareClass": 1, "CabinClass": 1, "Price": 22}]},
    {"Direction": 0, "TrainType": 6, "Fares": [{"TicketType": 1, "FareClass": 1, "CabinClass": 1, "Price": 1343}]},  # 環島另一邊
    {"Direction": 1, "TrainType": 3, "Fares": [{"TicketType": 1, "FareClass": 1, "CabinClass": 1, "Price": 34}]},  # 自強號
]}


class TdxFetcher:
    """依網址裡的 API 名稱回應手做的資料；fail 裡的 API 名稱會丟出 FetchError。"""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.requested: list[str] = []
        self.headers: list[dict | None] = []

    def post(self, url, data):
        assert data["grant_type"] == "client_credentials"
        return json.dumps({"access_token": "token-123", "expires_in": 86400})

    def get(self, url, cache_days=0, headers=None):
        self.requested.append(url)
        self.headers.append(headers)
        for name, body in (("StopOfRoute", ROUTES), ("Schedule", SCHEDULES), ("RouteFare", ROUTE_FARES),
                           ("DailyTrainTimetable", TIMETABLE), ("ODFare", TRA_FARES)):
            if f"/{name}/" in url:
                if name in self.fail:
                    raise FetchError(f"{url} 回應 503")
                return json.dumps(body)
        raise FetchError(f"{url} 回應 404")


def _collect(links, fetcher=None, stations=(A, B, ZHONGLI, TAOYUAN)):
    network = tdx.Network({s.id: s for s in stations}, links)
    collector = tdx.TdxCollector(network, fetcher=fetcher or TdxFetcher(), client_id="id", client_secret="secret", today=DAY)
    return collector.collect()


BUS_LINK = tdx.NetLink("tyn-a", "tyn-b", "公車", "公車（甲—乙）")
RAIL_LINK = tdx.NetLink("tyn-taoyuan", "tyn-zhongli", "台鐵", "台鐵縱貫線")


# ---------- 班次計算 ----------


def test_lonely_late_departure_is_not_the_last_bus():
    deps = [to_min(t) for t in ("18:00", "18:15", "18:30", "18:45", "21:00")]
    assert tdx._schedule(deps) == (to_min("18:00"), to_min("18:45"), 15)


def test_after_midnight_counts_as_the_night_before():
    assert tdx._service_min(to_min("00:20")) == to_min("24:20")
    assert tdx._service_min(to_min("05:00")) == to_min("05:00")


def test_days_are_combined_conservatively():
    weekday, weekend = (360, 1380, 10), (390, 1320, 20)
    assert tdx._combine_days([weekday, weekend]) == (390, 1320, 20)


def test_loop_routes_are_not_candidates():
    names = {c.name for c in tdx.bus_candidates(ROUTES, A, B)}
    assert names == {"R1", "R2"}
    assert tdx.bus_candidates(ROUTES, B, A) == []  # 這幾條路線只往乙站開


# ---------- 抓取 ----------


def test_bus_routes_on_the_same_stretch_are_merged():
    batch = _collect([BUS_LINK])
    [link] = batch.links
    assert link.mode == "公車" and link.line == "公車（R1、R2）"  # 班次多的 R1 排前面，環狀線不算
    assert (link.first_min, link.last_min) == (to_min("06:00"), to_min("22:00"))  # 23:59 那班太孤單，不當末班車
    assert link.headway_min == 20  # 平日合併後 10 分鐘一班，但週末只有 R1，取保守的
    assert link.ride_min == tdx._path_minutes(STRAIGHT, 0, 2)
    assert [(f.line, f.fare) for f in batch.fares] == [("公車（R1、R2）", 18)]
    assert batch.replace_tdx and batch.stats["bus_links"] == 1


def test_route_fares_are_fetched_only_for_the_routes_in_use():
    fetcher = TdxFetcher()
    _collect([BUS_LINK], fetcher)
    [fare_url] = [u for u in fetcher.requested if "/RouteFare/" in u]
    assert "R1" in fare_url and "R2" in fare_url and "LOOP" not in fare_url


def test_requests_carry_the_token():
    fetcher = TdxFetcher()
    _collect([BUS_LINK], fetcher)
    assert all(h == {"authorization": "Bearer token-123"} for h in fetcher.headers)


def test_rail_uses_every_train_and_the_regional_fare():
    batch = _collect([RAIL_LINK])
    [link] = batch.links
    assert link.line == "台鐵縱貫線" + tdx.RAIL_SUFFIX  # 和手寫的路段不撞名
    assert (link.ride_min, link.headway_min) == (10, 15)
    assert (link.first_min, link.last_min) == (to_min("05:30"), 24 * 60)
    assert [f.fare for f in batch.fares] == [22]  # 區間車、近的那一邊


def test_stretches_without_tdx_data_keep_the_estimate():
    batch = _collect([tdx.NetLink("tyn-b", "tyn-a", "公車", "公車（乙—甲）")])
    assert batch.links == [] and batch.stats["unmatched_links"] == 1


def test_missing_keys_stop_before_any_request():
    fetcher = TdxFetcher()
    collector = tdx.TdxCollector(tdx.Network({}, []), fetcher=fetcher, client_id="", client_secret="")
    with pytest.raises(ScrapeError, match="TDX 金鑰"):
        collector.collect()
    assert fetcher.requested == []


def test_any_failed_request_keeps_the_old_data():
    with pytest.raises(ScrapeError, match="TDX 查詢失敗"):
        _collect([BUS_LINK], TdxFetcher(fail={"Schedule"}))


def test_network_is_read_from_the_skeleton(session):
    load_network(session)
    collector = make_collector("tdx", session)
    assert {link.mode for link in collector.network.links} == {"台鐵", "公車"}
    assert len(collector.network.links) == 18  # 手寫的 9 條路線，兩個方向
    assert collector.network.stations["tyn-zhongli"].name == "中壢"


def test_run_tdx_into_the_database(session):
    """手寫骨架的中壢↔桃園台鐵，換成 TDX 的實際班次（另存一筆 source = tdx）。"""
    load_network(session)
    network = tdx.load_network(session)
    network.links = [l for l in network.links if l.from_id == "tyn-taoyuan" and l.to_id == "tyn-zhongli"]
    collector = tdx.TdxCollector(network, fetcher=TdxFetcher(), client_id="id", client_secret="secret", today=DAY)
    run = run_collector(session, collector)
    assert run.status == "success", run.error
    assert run.stats["rail_links"] == 1 and run.stats["transit_fares"] == 1
    line = "台鐵縱貫線" + tdx.RAIL_SUFFIX
    assert session.scalar(select(TransitLink.headway_min).where(TransitLink.line == line)) == 15
    assert session.scalar(select(TransitFare.fare).where(TransitFare.line == line)) == 22
    assert session.scalar(select(func.count()).select_from(TransitLink).where(TransitLink.source == "base")) == 18


def test_missing_keys_fail_the_run_and_keep_old_data(session):
    load_network(session)
    run = run_collector(session, make_collector("tdx", session))  # 測試裡沒有金鑰
    assert run.status == "failed" and "TDX 金鑰" in run.error

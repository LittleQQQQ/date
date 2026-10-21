"""交通部 TDX 運輸資料流通服務（https://tdx.transportdata.tw/）：台鐵、公車的班表與票價（規格書 v0.2 3.2）。

手寫的 data/network/ 是路網骨架（有哪些站、哪兩站之間有車）。這裡替骨架裡的台鐵、公車路段找真實資料，
存成 source = tdx 的路段與車資；排程時同一段、同一種交通方式以 TDX 為準，找不到資料的路段沿用手寫估計值。
規劃時不呼叫 TDX：每週由 refresh_transit 工作抓一次。

- 台鐵：當天所有列車（所有車種）的時刻，算出兩站之間的車程（中位數）、首末班、最長班距；票價用區間車成人票。
- 公車：TDX 只有起點站的發車時刻，桃園也沒有站間車程（S2STravelTime），所以車程依站牌之間的距離估計（BUS_KMH）；
  路線、發車時刻、上下車站之間的票價是真實資料。同一段有好幾條路線時合併成一段：哪一班先來就搭哪一班，
  班距用所有路線合併後的發車間隔，車程取最慢的那條（繞太遠的路線不算）。
- 平日、假日的公車班表不同，首末班與班距取一週裡最保守的（最晚的首班、最早的末班、最長的班距）。
- 捷運、高鐵還沒接，沿用估計值。路網只有桃園市，公車都查桃園市的資料。
- 免費方案每把金鑰每分鐘最多呼叫 5 次，所以每次請求間隔 MIN_INTERVAL 秒；更新一次約 5 次呼叫。
- 每月點數（quota:tdx_points，規格書 5.5）：這裡只用基礎 API，每次 2 點，呼叫成功才計。點數不夠就整批不更新，
  路網保留上次的資料。換 token 不算點數。
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..geo import distance_m
from ..models import Station, TransitLink, lat_of, lng_of
from ..redis_utils import get_redis
from ..redis_utils.lock import RedisLock
from ..redis_utils.quota import MonthlyQuota
from ..redis_utils.ratelimit import tdx_limiter
from ..timeutil import today_tw
from .base import Batch, Collector, FareRecord, LinkRecord, ScrapeError
from .http import Fetcher, FetchError

AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
API_URL = "https://tdx.transportdata.tw/api/basic/"
MIN_INTERVAL = 12.5  # 秒；每分鐘 5 次的上限再留一點餘裕
BASIC_POINTS = 2  # 基礎 API（/api/basic/）每次呼叫的點數

MODES = ("台鐵", "公車")
RAIL_SUFFIX = "（實際班次）"  # TDX 台鐵路段的路線名稱 = 骨架的路線名稱＋這個，不會和手寫的撞名
REGIONAL_TRAIN_TYPES = (6, 10)  # TDX 票價的車種代碼：6 區間車、10 區間快車

STOP_RADIUS_M = 250  # 公車站牌離骨架的車站多近才算同一站
BUS_KMH = 18  # 市區公車含停靠的平均速度，用來由站牌距離估車程
# 繞路太遠的路線（環狀線繞一大圈）不算：車程超過「兩站直線距離的車程 × 2.5 ＋ 5 分鐘」
DETOUR_MAX = 2.5
DETOUR_MAX_MIN = 5
MAX_LINE_NAMES = 3  # 合併的路線名稱最多列幾條

DAY_START = 4 * 60  # 凌晨 4 點前發車的算前一天的末班車（00:20 → 24:20）
HEADWAY_FROM = 10 * 60  # 班距只看 10 點以後（網頁最早出門 10:00），清晨的稀疏班次不算
TAIL_GAP_MIN = 30  # 末班前的間隔超過平常班距兩倍、也超過這個分鐘數，就不把那班當末班車（見 _schedule）

BUS_CITY = "Taoyuan"  # TDX 公車 API 的 City 參數；路網只有桃園市
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# ---------- 路網骨架 ----------


@dataclass(frozen=True)
class NetStation:
    id: str  # 路網骨架的車站代碼（stations.code）
    name: str
    lat: float
    lng: float


@dataclass(frozen=True)
class NetLink:
    from_id: str
    to_id: str
    mode: str
    line: str


@dataclass
class Network:
    stations: dict[str, NetStation]
    links: list[NetLink] = field(default_factory=list)


@dataclass(frozen=True)
class Point:
    lat: float
    lng: float


def load_network(session: Session) -> Network:
    """骨架：手寫的台鐵、公車路段（source = base）。"""
    rows = session.execute(select(Station.id, Station.code, Station.name, lat_of(Station.geom), lng_of(Station.geom))).all()
    codes = {sid: code for sid, code, *_ in rows}
    stations = {code: NetStation(code, name, lat, lng) for _, code, name, lat, lng in rows}
    links = [
        NetLink(codes[link.from_station_id], codes[link.to_station_id], link.mode, link.line)
        for link in session.scalars(select(TransitLink).where(TransitLink.source == "base", TransitLink.mode.in_(MODES)))
    ]
    return Network(stations, links)


# ---------- 時間 ----------


def _clock(text: str) -> int:
    h, m = text.split(":")[:2]
    return int(h) * 60 + int(m)


def _service_min(minutes: int) -> int:
    """凌晨 4 點前算前一天的延續：00:20 → 24:20。"""
    return minutes + 24 * 60 if minutes < DAY_START else minutes


def _schedule(departures: list[int]) -> tuple[int, int, int] | None:
    """一天的發車時刻 → (首班, 末班, 最長班距)；沒有班次回傳 None。

    排程整天只用一個班距，所以末班前零星的深夜班次（例如 21:00 之後隔兩小時才有 23:00 一班）
    會把班距拉得很大。這種班次從後面拿掉、末班車改成前一班：寧可說末班車早一點，也不要叫人等兩小時。
    """
    if not departures:
        return None
    deps = sorted(departures)
    gaps = [b - a for a, b in zip(deps, deps[1:]) if b > HEADWAY_FROM]
    while len(gaps) >= 2 and gaps[-1] > max(TAIL_GAP_MIN, 2 * statistics.median(gaps[:-1])):
        deps.pop()
        gaps.pop()
    return deps[0], deps[-1], max(gaps, default=60)


def _combine_days(days: list[tuple[int, int, int]]) -> tuple[int, int, int] | None:
    """一週各天的 (首班, 末班, 班距) 取最保守的：最晚的首班、最早的末班、最長的班距。"""
    first, last, headway = max(d[0] for d in days), min(d[1] for d in days), max(d[2] for d in days)
    return (first, last, headway) if first <= last else None


# ---------- 台鐵 ----------


def _norm(name: str) -> str:
    """台鐵站名比對用：「臺北」「台北車站」「台北站」都算台北。"""
    return name.replace("臺", "台").removesuffix("車站").removesuffix("站")


def rail_trips(timetable: dict, from_id: str, to_id: str) -> list[tuple[int, int]]:
    """當天從 from 站停靠、之後停 to 站的列車：[(from 站發車, 車程分鐘)]。"""
    trips = []
    for train in timetable.get("TrainTimetables", []):
        stops = {s["StationID"]: s for s in train["StopTimes"]}
        a, b = stops.get(from_id), stops.get(to_id)
        if not a or not b or a["StopSequence"] >= b["StopSequence"]:
            continue
        dep, arr = _clock(a["DepartureTime"]), _clock(b["ArrivalTime"])
        trips.append((_service_min(dep), (arr - dep) % (24 * 60)))
    return trips


def regional_fare(od_fares: dict) -> int | None:
    """台鐵兩站之間的區間車成人全票；TDX 會列出環島兩個方向，取便宜的（近的那一邊）。"""
    prices = [
        p["Price"]
        for od in od_fares.get("ODFares", [])
        if od.get("TrainType") in REGIONAL_TRAIN_TYPES
        for p in od.get("Fares", [])
        if p.get("TicketType") == 1 and p.get("FareClass") == 1 and p.get("CabinClass", 1) == 1
    ]
    return min(prices) if prices else None


# ---------- 公車 ----------


def _point(stop: dict) -> Point:
    pos = stop["StopPosition"]
    return Point(pos["PositionLat"], pos["PositionLon"])


def _path_minutes(stops: list[dict], i: int, j: int) -> int:
    """沿著站序從第 i 站開到第 j 站要幾分鐘（站牌間的直線距離加總 ÷ BUS_KMH）。"""
    meters = sum(distance_m(_point(stops[k]), _point(stops[k + 1])) for k in range(i, j))
    return round(meters / 1000 / BUS_KMH * 60)


def _nearest_stop(stops: list[dict], station: NetStation, start: int = 0) -> int | None:
    best = None
    for k in range(start, len(stops)):
        d = distance_m(_point(stops[k]), station)
        if d <= STOP_RADIUS_M and (best is None or d < best[0]):
            best = (d, k)
    return best[1] if best else None


@dataclass
class BusCandidate:
    """一條公車路線（子路線＋方向）怎麼服務骨架的一段：第 board 站上車、第 alight 站下車。"""

    route: dict  # TDX StopOfRoute 的一筆
    board: int
    alight: int
    ride: int

    @property
    def name(self) -> str:
        return self.route["RouteName"]["Zh_tw"]

    @property
    def key(self) -> tuple[str, int]:
        return self.route["SubRouteUID"], self.route["Direction"]

    @property
    def fare_key(self) -> tuple[str, int, str, str]:
        stops = self.route["Stops"]
        return self.route["SubRouteID"], self.route["Direction"], stops[self.board]["StopID"], stops[self.alight]["StopID"]


def bus_candidates(routes: list[dict], a: NetStation, b: NetStation) -> list[BusCandidate]:
    """從 a 站附近上車、之後在 b 站附近下車的路線；繞太遠的（環狀線繞一大圈）不算。

    繞路是跟兩站的直線距離比，不是跟最快的路線比：最快的路線可能一天只有幾班，不能拿它當標準。
    """
    limit = distance_m(a, b) / 1000 / BUS_KMH * 60 * DETOUR_MAX + DETOUR_MAX_MIN
    found = []
    for route in routes:
        stops = route.get("Stops") or []
        board = _nearest_stop(stops, a)
        if board is None:
            continue
        alight = _nearest_stop(stops, b, board + 1)
        if alight is None:
            continue
        ride = max(1, _path_minutes(stops, board, alight))
        if ride <= limit:
            found.append(BusCandidate(route, board, alight, ride))
    return found


def bus_departures(candidate: BusCandidate, schedule: dict | None) -> dict[int, list[int]]:
    """這條路線在上車站的發車時刻，依星期分開：{0（星期一）: [分鐘, ...], ...}。

    TDX 的班表只有起點站（通常是第 1 站）的發車時間，到上車站的時間依站牌距離推算。
    """
    by_day: dict[int, list[int]] = defaultdict(list)
    if not schedule:
        return by_day
    stops = candidate.route["Stops"]
    seq_index = {s["StopSequence"]: k for k, s in enumerate(stops)}
    for trip in schedule.get("Timetables") or []:
        times = trip.get("StopTimes") or []
        if not times:
            continue
        start = seq_index.get(times[0].get("StopSequence"), 0)
        if start > candidate.board:
            continue
        dep = _service_min(_clock(times[0]["DepartureTime"]) + _path_minutes(stops, start, candidate.board))
        service = trip.get("ServiceDay")
        for day, name in enumerate(WEEKDAYS):
            if service is None or service.get(name):
                by_day[day].append(dep)
    return by_day


def bus_line_name(candidates: list[BusCandidate], counts: Counter) -> str:
    """「公車（170、5087、5089 等）」：班次多的路線排前面。"""
    names = sorted({c.name for c in candidates}, key=lambda n: (-counts[n], n))
    shown = "、".join(names[:MAX_LINE_NAMES]) + (" 等" if len(names) > MAX_LINE_NAMES else "")
    return f"公車（{shown}）"


def bus_fare_index(route_fares: list[dict]) -> tuple[dict[tuple, int], set[str]]:
    """TDX RouteFare → ({(子路線, 方向, 上車站, 下車站): 成人全票}, 免費公車的子路線)。"""
    index: dict[tuple, int] = {}
    free: set[str] = set()
    for rec in route_fares:
        if rec.get("IsFreeBus"):
            free.add(rec["SubRouteID"])
        for od in rec.get("ODFares") or []:
            price = next(
                (p["Price"] for p in od.get("Fares", []) if p.get("TicketType") == 1 and p.get("FareClass") == 1), None
            )
            if price is not None:
                key = (rec["SubRouteID"], od["Direction"], od["OriginStop"]["StopID"], od["DestinationStop"]["StopID"])
                index[key] = price
    return index, free


def bus_fare(candidates: list[BusCandidate], index: dict[tuple, int], free: set[str]) -> int | None:
    """合併路段的車資：各路線票價裡最常見的（一樣多取貴的）；都查不到回傳 None（改用估計）。"""
    prices = []
    for c in candidates:
        if c.route["SubRouteID"] in free:
            prices.append(0)
        elif (price := index.get(c.fare_key)) is not None:
            prices.append(price)
    if not prices:
        return None
    counts = Counter(prices)
    return max(prices, key=lambda p: (counts[p], p))


# ---------- token 共用（規格書 5.3 A1） ----------

TOKEN_KEY = "tdx:access_token"
TOKEN_LOCK = "lock:tdx:token"
TOKEN_MARGIN_S = 5 * 60  # 比 TDX 給的有效期限早 5 分鐘過期，免得用到剛好過期的 token
TOKEN_WAIT_S = 30


def shared_token(request: Callable[[], tuple[str, int]], client: redis.Redis,
                 sleep: Callable[[float], None] = time.sleep) -> str:
    """先讀 Redis；沒有就搶 lock:tdx:token，搶到的去換新 token 寫回，沒搶到的等 1 秒再讀。

    換 token 的端點每個 IP 每分鐘最多 20 次，多個實例同時啟動時不能每個都去換。
    """
    for _ in range(TOKEN_WAIT_S):
        if token := client.get(TOKEN_KEY):
            return token
        lock = RedisLock(TOKEN_LOCK, 30_000, client)
        if lock.acquire():
            try:
                token, expires_in = request()
                client.set(TOKEN_KEY, token, ex=max(60, expires_in - TOKEN_MARGIN_S))
                return token
            finally:
                lock.release()
        sleep(1)
    raise ScrapeError(f"等了 {TOKEN_WAIT_S} 秒還拿不到 TDX token")


# ---------- 資料來源 ----------


class TdxCollector(Collector):
    name = "tdx"

    def __init__(
        self,
        network: Network,
        fetcher: Fetcher | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        today: dt.date | None = None,
        redis_client: redis.Redis | None = None,
    ):
        self.network = network
        self.fetcher = fetcher or Fetcher(cache_dir=None, min_interval=MIN_INTERVAL, timeout=120, backoff=30)
        self.client_id = settings.tdx_client_id if client_id is None else client_id
        self.client_secret = settings.tdx_client_secret if client_secret is None else client_secret
        self.today = today
        self.redis = redis_client or get_redis()
        self.limiter = tdx_limiter(self.redis)  # 所有程式實例共用每分鐘 5 次
        self.points = MonthlyQuota("tdx_points", settings.tdx_monthly_points_limit, self.redis)
        self._headers: dict[str, str] = {}

    @classmethod
    def create(cls, session: Session) -> TdxCollector:
        return cls(load_network(session))

    def collect(self) -> Batch:
        if not (self.client_id.strip() and self.client_secret.strip()):
            raise ScrapeError("沒有設定 TDX 金鑰（.env 的 TDX_CLIENT_ID、TDX_CLIENT_SECRET）")
        links: list[LinkRecord] = []
        fares: list[FareRecord] = []
        stats: Counter = Counter()
        try:
            self._login()
            rail = [link for link in self.network.links if link.mode == "台鐵"]
            if rail:
                self._rail(rail, self.today or today_tw(), links, fares, stats)
            bus = [link for link in self.network.links if link.mode == "公車"]
            if bus:
                self._bus(bus, links, fares, stats)
        except FetchError as exc:  # 一次只抓幾份資料，有一份失敗就整批不更新，保留上次的
            raise ScrapeError(f"TDX 查詢失敗：{exc}") from exc
        return Batch(links=links, fares=fares, replace_tdx=True, stats=dict(stats))

    def _login(self) -> None:
        self._headers = {"authorization": f"Bearer {shared_token(self._request_token, self.redis)}"}

    def _request_token(self) -> tuple[str, int]:
        text = self.fetcher.post(AUTH_URL, {
            "grant_type": "client_credentials", "client_id": self.client_id.strip(),
            "client_secret": self.client_secret.strip(),
        })
        data = json.loads(text)
        return data["access_token"], int(data.get("expires_in", 86400))

    def _get(self, path: str):
        if not self.points.available(BASIC_POINTS):  # 點數用完：整批不更新，保留上次的資料
            raise ScrapeError(f"這個月的 TDX 點數用完了（上限 {self.points.limit} 點），等下個月再更新班表")
        self.limiter.wait()  # 碰到每分鐘 5 次的上限就排隊等（規格書 5.5）
        text = self.fetcher.get(API_URL + path, headers=self._headers)
        self.points.consume(BASIC_POINTS)
        return json.loads(text)

    def _link(self, link: NetLink, line: str, ride: int, timing: tuple[int, int, int]) -> LinkRecord:
        first, last, headway = timing
        return LinkRecord(
            from_code=link.from_id, to_code=link.to_id, mode=link.mode, line=line,
            ride_min=ride, headway_min=headway, first_min=first, last_min=last,
        )

    def _rail(self, rail: list[NetLink], day: dt.date, links: list, fares: list, stats: Counter) -> None:
        timetable = self._get(f"v3/Rail/TRA/DailyTrainTimetable/TrainDate/{day.isoformat()}?%24format=JSON")
        ids: dict[str, str] = {}  # 站名 → 台鐵車站代碼
        for train in timetable.get("TrainTimetables", []):
            for stop in train["StopTimes"]:
                ids.setdefault(_norm(stop["StationName"]["Zh_tw"]), stop["StationID"])
        pair_fares: dict[frozenset, int | None] = {}
        for link in rail:
            a, b = (ids.get(_norm(self.network.stations[s].name)) for s in (link.from_id, link.to_id))
            trips = rail_trips(timetable, a, b) if a and b else []
            timing = _schedule([dep for dep, _ in trips])
            if timing is None:
                stats["unmatched_links"] += 1
                continue
            line = link.line + RAIL_SUFFIX
            links.append(self._link(link, line, round(statistics.median(r for _, r in trips)), timing))
            stats["rail_links"] += 1
            pair = frozenset((a, b))
            if pair not in pair_fares:  # 票價兩個方向一樣，一對車站只查一次
                pair_fares[pair] = regional_fare(self._get(f"v3/Rail/TRA/ODFare/{a}/to/{b}?%24format=JSON"))
            if (fare := pair_fares[pair]) is not None:
                fares.append(FareRecord(from_code=link.from_id, to_code=link.to_id, line=line, fare=fare))

    def _bus(self, bus: list[NetLink], links: list, fares: list, stats: Counter) -> None:
        routes = self._get(f"v2/Bus/StopOfRoute/City/{BUS_CITY}?%24format=JSON")
        schedules = {(s["SubRouteUID"], s["Direction"]): s for s in self._get(f"v2/Bus/Schedule/City/{BUS_CITY}?%24format=JSON")}
        found = []  # (骨架路段, 路線, 合併後的時刻, 路線名稱)
        for link in bus:
            a, b = self.network.stations[link.from_id], self.network.stations[link.to_id]
            candidates, days, counts = [], defaultdict(list), Counter()
            for c in bus_candidates(routes, a, b):
                deps = bus_departures(c, schedules.get(c.key))
                if not deps:
                    continue
                candidates.append(c)
                for day, times in deps.items():
                    days[day].extend(times)
                counts[c.name] += sum(len(t) for t in deps.values())
            per_day = [timing for day in range(7) if (timing := _schedule(days[day])) is not None]
            timing = _combine_days(per_day) if per_day else None
            if timing is None:
                stats["unmatched_links"] += 1
                continue
            found.append((link, candidates, timing, bus_line_name(candidates, counts)))
        if not found:
            return
        # 票價資料很大（桃園全部約 160 MB），只查用得到的路線
        names = sorted({c.name for _, cands, _, _ in found for c in cands})
        query = " or ".join(f"RouteName eq '{n}'" for n in names)
        index, free = bus_fare_index(self._get(f"v2/Bus/RouteFare/City/{BUS_CITY}?%24filter={quote(query)}&%24format=JSON"))
        for link, candidates, timing, line in found:
            links.append(self._link(link, line, max(c.ride for c in candidates), timing))
            stats["bus_links"] += 1
            if (fare := bus_fare(candidates, index, free)) is not None:
                fares.append(FareRecord(from_code=link.from_id, to_code=link.to_id, line=line, fare=fare))

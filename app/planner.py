"""排程核心（程式計算）。

把「場次 × 餐廳 × 先吃或後吃」全部列出來，逐段檢查時間接不接得上，再挑出 3 個方案。
這個檔案不碰資料庫、也不直接呼叫 Jev：資料和 Jev 的判斷由 services.py 準備好傳進來，方便單獨測試。
"""
from __future__ import annotations

import hashlib
import itertools
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from .fares import leg_fare, route_fare
from .geo import walk_minutes
from .jev.questions import (
    DATE_FIT,
    MEAL_MINUTES,
    JevAnswer,
    Question,
    movie_choice_question,
    restaurant_choice_question,
    transit_choice_question,
)
from .schemas import PlanRequest
from .timeutil import fmt, to_min
from .transit import Route, TransitGraph

PRE_SHOW_MIN = 10  # 開場前幾分鐘要到影城
ADS_MIN = 12  # 開場後的廣告與預告
DINNER_BUFFER_MIN = 10  # 先吃飯時，預留提早離開餐廳的彈性
STATION_WALK_MAX = 15  # 車站到影城、餐廳最多走幾分鐘；再遠就該轉公車
PEAK_START, PEAK_END = to_min("17:30"), to_min("20:30")
MAX_CONFIRMATIONS = 5

ASSUMPTIONS = {
    "開場前到影城": f"{PRE_SHOW_MIN} 分鐘",
    "廣告與預告": f"{ADS_MIN} 分鐘",
    "先吃飯時預留": f"{DINNER_BUFFER_MIN} 分鐘",
    "用餐時間": "快速 40 分、一般 60 分、慢食 90 分（快慢由 Jev 判斷）",
    "步行時間": f"直線距離 × 1.3，每分鐘 75 公尺；車站到影城或餐廳最多走 {STATION_WALK_MAX} 分鐘",
    "等車時間": "以班距估最久要等多久；換線另加各站的轉乘步行時間",
    "候位時間": "尖峰（17:30–20:30）週五到週日 ×1.5、離峰 ×0.5；可訂位的餐廳假設已訂位",
    "車資": "依交通方式和車程估計：捷運 20～65 元、市區公車 15 元、國道客運每分鐘約 2.3 元、台鐵每分鐘約 2.5 元、高鐵每分鐘約 13 元；不是實際票價",
    "預算": "每人預算包含電影票、餐費與來回車資",
    "怎麼選": "程式先篩出時間、交通、預算都接得起來的組合；看哪部片、吃哪一家由 Jev 選，前三名方案的去程、回程搭什麼也由 Jev 從接得上、付得起的走法裡選；Jev 沒把握時先請本機 LLM 幫忙判斷（JEV_LLM_ASSIST）；同一組片和餐廳有好幾個場次時，程式挑緩衝最充裕的",
}


@dataclass
class Place:
    id: int | str
    name: str
    lat: float
    lng: float


@dataclass
class ShowtimeInfo:
    id: int
    cinema: Place
    movie_title: str
    raw_title: str
    runtime_min: int
    start: int
    label: str
    price: int
    screen: JevAnswer
    audio: JevAnswer
    title_match: JevAnswer | None = None
    genre: str = ""
    synopsis: str = ""
    estimated: list[str] = field(default_factory=list)  # 估計的欄位，例如 ["price"]


@dataclass
class RestaurantInfo:
    place: Place
    price: int
    open: int
    close: int
    last_order_min: int
    typical_wait_min: int
    reservable: bool
    date_fit: JevAnswer
    pace: JevAnswer
    cuisine: JevAnswer
    description: str = ""
    reviews: list[str] = field(default_factory=list)
    diet_ok: JevAnswer | None = None  # 使用者有飲食限制時才問
    estimated: list[str] = field(default_factory=list)  # 估計的欄位，例如 ["hours", "price"]
    url: str = ""  # 店家頁面（Google 地圖或 OSM）


# 問 Jev 的介面：（題目, 對象, 要給 Jev 讀的資料）→ 答案（已套用使用者確認過的答案）
AskJev = Callable[[Question, str, dict], JevAnswer]


@dataclass
class PlanContext:
    origin: Place  # 出發站
    area_stations: list[Place]  # 服務這個商圈的站，從這些站步行到影城、餐廳
    graph: TransitGraph
    showtimes: list[ShowtimeInfo]
    restaurants: list[RestaurantInfo]
    weekend: bool
    threshold: float
    ask: AskJev


@dataclass
class Step:
    start: int
    end: int | None
    kind: str  # transit / walk / buffer / movie / wait / meal / arrive
    title: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "time": fmt(self.start),
            "end": fmt(self.end) if self.end is not None else None,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
        }


@dataclass
class TripOption:
    """一個方向（去程或回程）的一種走法。"""

    route: Route
    station: Place  # 商圈這一頭的車站
    walk: int  # 這個車站和影城或餐廳之間走幾分鐘
    fare: int  # 每人車資（估計）

    @property
    def label(self) -> str:
        return self.route.label


@dataclass(frozen=True)
class RouteChoice:
    """排一個組合時怎麼挑走法。every：列出所有走法（不只最快的）；go、back：指定的走法名稱，None 是預算內最快的。"""

    every: bool = False
    go: str | None = None
    back: str | None = None


class TripSearch:
    """一個方向的走法，由快到慢。先只找最快的；超出預算、或要給 Jev 選時，才找其他走法（要多算好幾次路線）。"""

    def __init__(self, find: Callable[[bool], list[TripOption]], every: bool):
        self._find = find
        self.every = every
        self.options = find(every)

    def expand(self) -> None:
        if not self.every:
            self.options, self.every = self._find(True), True


@dataclass
class Candidate:
    order: str  # movie_first / dinner_first
    showtime: ShowtimeInfo
    restaurant: RestaurantInfo
    steps: list[Step]
    leave_home: int
    arrive_home: int
    film_end: int
    cost: int
    tightest: int  # 所有關鍵銜接中最小的緩衝分鐘數
    walk_total: int
    wait: int
    go: TripOption  # 去程搭哪一種
    back: TripOption  # 回程搭哪一種
    go_options: list[TripOption]  # 找過的去程走法，由快到慢（只找最快的時只有一個）
    back_options: list[TripOption]
    flags: list[str] = field(default_factory=list)  # 風險
    tips: list[str] = field(default_factory=list)  # 提醒


class Reject(Exception):
    """這個組合接不起來；訊息就是原因，會統計後顯示給使用者。"""


# ---------- 交通 ----------


def station_name(name: str) -> str:
    """「板橋」→「板橋站」；「台北車站」已經有站字，不再加。"""
    return name if name.endswith("站") else f"{name}站"


def _nearby_stations(ctx: PlanContext, place: Place) -> list[tuple[Place, int]]:
    stations = [(s, walk_minutes(s, place)) for s in ctx.area_stations]
    nearby = [(s, walk) for s, walk in stations if walk <= STATION_WALK_MAX]
    if not nearby:
        raise Reject(f"離最近的車站要走超過 {STATION_WALK_MAX} 分鐘")
    return nearby


# 找替代走法時要拿掉的交通方式：{高鐵, 台鐵, 公車} 的所有子集；第一個是空集合（完整路網，找最快的）
EXCLUDED_MODES = [frozenset(c) for n in range(4) for c in itertools.combinations(("高鐵", "台鐵", "公車"), n)]
MAX_TRIP_OPTIONS = 4  # 每個方向最多給 Jev 幾種走法


def _distinct(options: list[TripOption]) -> list[TripOption]:
    """options 已由快到慢排好。同名的走法只留較快的；比前面的慢、又沒有比較便宜的刪掉；最多 MAX_TRIP_OPTIONS 種。"""
    kept: list[TripOption] = []
    for o in options:
        if all(o.label != k.label and o.fare < k.fare for k in kept):
            kept.append(o)
    return kept[:MAX_TRIP_OPTIONS]


def _out_options(ctx: PlanContext, req: PlanRequest, place: Place, arrive_by: int, every: bool) -> list[TripOption]:
    """去程的走法：要在 arrive_by 走到 place，由快到慢（越晚出門越前面）。every=False 只找最快的。"""
    nearby = _nearby_stations(ctx, place)
    found: list[TripOption] = []
    for modes in EXCLUDED_MODES if every else EXCLUDED_MODES[:1]:
        graph = ctx.graph.excluding(modes)
        best: TripOption | None = None
        for station, walk in nearby:
            route = graph.latest_departure(ctx.origin.id, station.id, arrive_by - walk)
            if route is not None and (best is None or route.depart > best.route.depart):
                best = TripOption(route, station, walk, route_fare(route))
        if best is None and not modes:
            raise Reject("去程沒有可以搭的班次")
        if best is not None:
            found.append(best)
    earliest = to_min(req.depart_after)
    if found[0].route.depart < earliest:  # 最快的走法都要更早出門
        raise Reject("要比你能出門的時間更早出發")
    usable = [o for o in found if o.route.depart >= earliest]
    return _distinct(sorted(usable, key=lambda o: (-o.route.depart, o.fare)))


def _home_options(ctx: PlanContext, req: PlanRequest, place: Place, ready: int, every: bool) -> list[TripOption]:
    """回程的走法：ready 時從 place 出發回出發站，由快到慢（越早到家越前面）。every=False 只找最快的。"""
    nearby = _nearby_stations(ctx, place)
    found: list[TripOption] = []
    for modes in EXCLUDED_MODES if every else EXCLUDED_MODES[:1]:
        graph = ctx.graph.excluding(modes)
        best: TripOption | None = None
        for station, walk in nearby:
            route = graph.earliest_arrival(station.id, ready + walk, ctx.origin.id)
            if route is not None and (best is None or route.arrive < best.route.arrive):
                best = TripOption(route, station, walk, route_fare(route))
        if best is None and not modes:
            raise Reject("回程趕不上末班車")
        if best is not None:
            found.append(best)
    home_by = to_min(req.home_by)
    if found[0].route.arrive > home_by:  # 最快的走法都太晚到家
        raise Reject(f"沒辦法在 {fmt(home_by)} 前到家")
    usable = [o for o in found if o.route.arrive <= home_by]
    return _distinct(sorted(usable, key=lambda o: (o.route.arrive, o.fare)))


def _affordable_go(
    req: PlanRequest, base_cost: int, gos: list[TripOption], backs: list[TripOption]
) -> list[TripOption]:
    """付得起的去程：加上最便宜的回程、電影票、餐費，不超過每人預算。"""
    if not req.budget_per_person:
        return list(gos)
    cheapest_back = min(o.fare for o in backs)
    return [o for o in gos if base_cost + o.fare + cheapest_back <= req.budget_per_person]


def _affordable_back(req: PlanRequest, base_cost: int, go: TripOption, backs: list[TripOption]) -> list[TripOption]:
    """付得起的回程：選好的去程之後，剩下的預算付得起的。"""
    if not req.budget_per_person:
        return list(backs)
    return [o for o in backs if base_cost + go.fare + o.fare <= req.budget_per_person]


def _pick_trips(
    req: PlanRequest, base_cost: int, go: TripSearch, back: TripSearch, choice: RouteChoice
) -> tuple[TripOption, TripOption]:
    """挑去程、回程：指定的走法（付得起的話），不然是預算內最快的；怎麼搭都超出預算就排除這個組合。"""
    if req.budget_per_person and base_cost > req.budget_per_person:
        raise Reject("超過每人預算")  # 電影加餐費就超出預算，不必再找走法
    if not _affordable_go(req, base_cost, go.options[:1], back.options[:1]):
        go.expand()  # 最快的走法超出預算，才找便宜的走法
        back.expand()
    gos = _affordable_go(req, base_cost, go.options, back.options)
    if not gos:
        raise Reject("超過每人預算")
    picked_go = next((o for o in gos if o.label == choice.go), gos[0])
    backs = _affordable_back(req, base_cost, picked_go, back.options)
    picked_back = next((o for o in backs if o.label == choice.back), backs[0])
    return picked_go, picked_back


def _transit_steps(route: Route, departing: bool) -> list[Step]:
    steps = []
    for i, leg in enumerate(route.legs):
        title = f"搭{leg.label}到{station_name(leg.to_name)}"
        if departing and i == 0:
            title = f"從{station_name(leg.from_name)}出發，{title}"
        detail = f"車程 {leg.ride_min} 分鐘，等車最久 {leg.wait} 分鐘，車資約 {leg_fare(leg.mode, leg.ride_min)} 元"
        if leg.transfer_min:
            detail = f"在{station_name(leg.from_name)}轉乘（步行 {leg.transfer_min} 分鐘）；{detail}"
        steps.append(Step(leg.ready - leg.transfer_min, leg.alight, "transit", title, detail))
    return steps


def _out_steps(ctx: PlanContext, place: Place, arrive_by: int, trip: TripOption) -> list[Step]:
    """去程的時間軸：搭車、步行到 place；比 arrive_by 早到時標出空檔。"""
    route, walk = trip.route, trip.walk
    steps = _transit_steps(route, departing=True)
    title = f"步行到{place.name}" if route.legs else f"從{station_name(ctx.origin.name)}步行到{place.name}"
    at_place = route.arrive + walk
    steps.append(Step(route.arrive, at_place, "walk", title, f"約 {walk} 分鐘"))
    if at_place < arrive_by:  # 等車以最壞情況估，通常會提早到
        steps.append(Step(at_place, arrive_by, "buffer", "提早抵達", f"多出 {arrive_by - at_place} 分鐘空檔"))
    return steps


def _home_steps(ctx: PlanContext, ready: int, trip: TripOption, flags: list[str]) -> list[Step]:
    """回程的時間軸：ready 時出發，步行到車站、搭車回出發站；接近末班車時加提醒。"""
    route, walk = trip.route, trip.walk
    if not route.legs:
        return [Step(ready, ready + walk, "walk", f"步行回{station_name(ctx.origin.name)}", f"約 {walk} 分鐘")]
    steps = [Step(ready, ready + walk, "walk", f"步行到{station_name(trip.station.name)}", f"約 {walk} 分鐘")]
    steps += _transit_steps(route, departing=False)
    for leg in route.legs:
        if leg.last - leg.ready < 15:
            flags.append(f"{leg.label}接近末班車（{station_name(leg.from_name)} {fmt(leg.last)} 發車）")
    return steps


# ---------- 餐廳 ----------


def _wait(ctx: PlanContext, r: RestaurantInfo, at: int) -> int:
    if r.reservable:
        return 0
    if PEAK_START <= at <= PEAK_END:
        return round(r.typical_wait_min * (1.5 if ctx.weekend else 1.0))
    return round(r.typical_wait_min * 0.5)


def _meal_step(r: RestaurantInfo, seat: int, meal_end: int, last_order: int) -> Step:
    return Step(seat, meal_end, "meal", f"在{r.place.name}用餐", f"{r.cuisine.top}・{r.pace.top}・最後點餐 {fmt(last_order)}")


def _movie_steps(s: ShowtimeInfo, arrive_cinema: int, film_end: int) -> list[Step]:
    return [
        Step(arrive_cinema, s.start, "buffer", "到影城取票", f"開場前 {PRE_SHOW_MIN} 分鐘到"),
        Step(s.start, film_end, "movie", f"《{s.movie_title}》{s.label}", f"開場後約 {ADS_MIN} 分鐘廣告，片長 {s.runtime_min} 分鐘"),
    ]


# ---------- 兩種順序 ----------


def _movie_first(
    ctx: PlanContext, req: PlanRequest, s: ShowtimeInfo, r: RestaurantInfo, choice: RouteChoice = RouteChoice()
) -> Candidate:
    flags: list[str] = []
    arrive_cinema = s.start - PRE_SHOW_MIN
    go = TripSearch(lambda every: _out_options(ctx, req, s.cinema, arrive_cinema, every), choice.every)
    film_end = s.start + ADS_MIN + s.runtime_min
    middle = _movie_steps(s, arrive_cinema, film_end)

    walk_cr = walk_minutes(s.cinema, r.place)
    arrive_r = film_end + walk_cr
    middle.append(Step(film_end, arrive_r, "walk", f"步行到{r.place.name}", f"約 {walk_cr} 分鐘"))
    ready = max(arrive_r, r.open)
    if ready > arrive_r:
        middle.append(Step(arrive_r, ready, "wait", "等餐廳開門"))
    wait = _wait(ctx, r, ready)
    seat = ready + wait
    if wait:
        middle.append(Step(ready, seat, "wait", "候位", f"預估 {wait} 分鐘"))
    last_order = r.close - r.last_order_min
    if seat > last_order:
        raise Reject("散場後趕不上餐廳最後點餐")
    meal_end = seat + MEAL_MINUTES[r.pace.top]
    if meal_end > r.close:
        raise Reject("用餐時間會超過餐廳打烊")
    middle.append(_meal_step(r, seat, meal_end, last_order))

    back = TripSearch(lambda every: _home_options(ctx, req, r.place, meal_end, every), choice.every)
    trips = _pick_trips(req, s.price + r.price, go, back, choice)
    steps = _out_steps(ctx, s.cinema, arrive_cinema, trips[0]) + middle + _home_steps(ctx, meal_end, trips[1], flags)
    order_slack = last_order - seat
    if order_slack < 15:
        flags.append(f"散場後到最後點餐只剩 {order_slack} 分鐘，候位久一點就吃不到")
    home_slack = to_min(req.home_by) - trips[1].route.arrive
    return _finish(
        ctx, req, "movie_first", s, r, steps, flags, trips, (go.options, back.options), film_end,
        trips[0].walk + walk_cr + trips[1].walk, wait, min(order_slack, home_slack),
    )


def _dinner_first(
    ctx: PlanContext, req: PlanRequest, s: ShowtimeInfo, r: RestaurantInfo, choice: RouteChoice = RouteChoice()
) -> Candidate:
    flags: list[str] = []
    arrive_cinema = s.start - PRE_SHOW_MIN
    walk_rc = walk_minutes(r.place, s.cinema)
    leave_r = arrive_cinema - walk_rc
    meal_end = leave_r - DINNER_BUFFER_MIN
    seat = meal_end - MEAL_MINUTES[r.pace.top]
    if seat < r.open:
        raise Reject("電影開演前餐廳還沒開")
    last_order = r.close - r.last_order_min
    if seat > last_order:
        raise Reject("趕不上餐廳最後點餐")
    if meal_end > r.close:
        raise Reject("用餐時間會超過餐廳打烊")
    wait = _wait(ctx, r, seat)
    arrive_r = seat - wait

    go = TripSearch(lambda every: _out_options(ctx, req, r.place, arrive_r, every), choice.every)
    middle: list[Step] = []
    if wait:
        middle.append(Step(arrive_r, seat, "wait", "候位", f"預估 {wait} 分鐘"))
    middle.append(_meal_step(r, seat, meal_end, last_order))
    middle.append(Step(meal_end, leave_r, "buffer", "預留彈性時間", f"上菜慢或結帳排隊都還來得及（{DINNER_BUFFER_MIN} 分鐘）"))
    middle.append(Step(leave_r, arrive_cinema, "walk", f"步行到{s.cinema.name}", f"約 {walk_rc} 分鐘"))
    film_end = s.start + ADS_MIN + s.runtime_min
    middle += _movie_steps(s, arrive_cinema, film_end)

    back = TripSearch(lambda every: _home_options(ctx, req, s.cinema, film_end, every), choice.every)
    trips = _pick_trips(req, s.price + r.price, go, back, choice)
    steps = _out_steps(ctx, r.place, arrive_r, trips[0]) + middle + _home_steps(ctx, film_end, trips[1], flags)
    connection = PRE_SHOW_MIN + DINNER_BUFFER_MIN
    if wait >= 15:
        flags.append(f"候位預估 {wait} 分鐘；比預估多等超過 {connection} 分鐘就會趕不上開場")
    home_slack = to_min(req.home_by) - trips[1].route.arrive
    return _finish(
        ctx, req, "dinner_first", s, r, steps, flags, trips, (go.options, back.options), film_end,
        trips[0].walk + walk_rc + trips[1].walk, wait, min(connection, home_slack),
    )


def _finish(
    ctx: PlanContext, req: PlanRequest, order: str, s: ShowtimeInfo, r: RestaurantInfo, steps: list[Step],
    flags: list[str], trips: tuple[TripOption, TripOption], options: tuple[list[TripOption], list[TripOption]],
    film_end: int, walk_total: int, wait: int, tightest: int,
) -> Candidate:
    go, back = trips
    arrive_home = back.route.arrive
    home_by = to_min(req.home_by)
    home_slack = home_by - arrive_home
    if home_slack < 15:
        flags.append(f"只比 {fmt(home_by)} 早 {home_slack} 分鐘到家")
    tips = []
    if r.reservable:
        tips.append("記得先訂位（已假設不用候位）")
    elif wait >= 20:
        flags.append(f"尖峰時段，候位預估 {wait} 分鐘")
    steps.append(Step(arrive_home, None, "arrive", f"回到{station_name(ctx.origin.name)}", f"比 {fmt(home_by)} 早 {home_slack} 分鐘"))
    return Candidate(
        order=order, showtime=s, restaurant=r, steps=steps, leave_home=go.route.depart, arrive_home=arrive_home,
        film_end=film_end, cost=s.price + r.price + go.fare + back.fare, tightest=tightest, walk_total=walk_total,
        wait=wait, go=go, back=back, go_options=options[0], back_options=options[1], flags=flags, tips=tips,
    )


BUILDERS = {"movie_first": _movie_first, "dinner_first": _dinner_first}


# ---------- 篩選、評分、挑選 ----------


def _ask(asks: dict[str, dict], ans: JevAnswer, question: str, priority: int, kind: str = "filter") -> None:
    """kind：filter＝Jev 沒把握就先不排這個選項，要使用者確認；choice＝Jev 照樣選，但列出其他選項讓使用者換。"""
    if ans.key not in asks:
        ranked = sorted(zip(ans.options, ans.probs), key=lambda x: -x[1])
        if kind == "choice":
            ranked = ranked[:4]
        asks[ans.key] = {
            "key": ans.key,
            "kind": kind,
            "question": question,
            "options": [option for option, _ in ranked] if kind == "choice" else ans.options,
            "probs": [round(p, 3) for _, p in ranked] if kind == "choice" else [round(p, 3) for p in ans.probs],
            "jev_guess": ans.top,
            "confidence": round(ans.confidence, 3),
            "priority": priority,
        }


def _keep_showtime(req: PlanRequest, s: ShowtimeInfo, thr: float, asks: dict, filtered: Counter) -> bool:
    checks: list[tuple[JevAnswer, Callable[[str], bool], str, int, str]] = []
    if req.movie_query and s.title_match is not None:
        checks.append((s.title_match, lambda top: top == "是", f"「{s.raw_title}」是你想看的「{req.movie_query}」嗎？", 0, "不是想看的片"))
    if req.screen_types:
        checks.append((s.screen, lambda top: top in req.screen_types, f"場次標示「{s.label}」是哪一種影廳？", 1, "影廳不符"))
    if req.audio:
        checks.append((s.audio, lambda top: top == req.audio, f"場次標示「{s.label}」是原音還是配音？", 1, "語言不符"))
    for ans, accept, question, priority, reason in checks:
        if ans.confident(thr):
            if not accept(ans.top):
                filtered[reason] += 1
                return False
        else:
            _ask(asks, ans, question, priority)
            filtered["Jev 沒把握，等你確認"] += 1
            return False
    return True


def _keep_restaurant(req: PlanRequest, r: RestaurantInfo, thr: float, asks: dict, filtered: Counter) -> bool:
    if r.date_fit.confident(thr) and r.date_fit.top == DATE_FIT.options[-1]:
        filtered["LLM 判斷不適合約會" if r.date_fit.source == "llm" else "Jev 判斷不適合約會"] += 1
        return False
    if req.cuisines:
        c = r.cuisine
        if c.confident(thr):
            if c.top not in req.cuisines:
                filtered["料理類型不符"] += 1
                return False
        elif sum(c.prob(x) for x in req.cuisines) >= 0.15:
            _ask(asks, c, f"「{r.place.name}」算是哪一類料理？", 2)
            filtered["Jev 沒把握，等你確認"] += 1
            return False
        else:
            filtered["料理類型不符"] += 1
            return False
    if req.diet and r.diet_ok is not None:
        ok = r.diet_ok
        if ok.confident(thr):
            if ok.top != "可以":
                filtered["不符合飲食限制"] += 1
                return False
        else:
            _ask(asks, ok, f"「{r.place.name}」能滿足「{req.diet}」嗎？", 2)
            filtered["Jev 沒把握，等你確認"] += 1
            return False
    return True


PLAN_COUNT = 3
TOP_MOVIES = 3  # 前幾名的電影要再請 Jev 各選一次餐廳
# 一題最多給 Jev 幾個選項。排得進去的片、配得上的餐廳更多時，程式先依使用者條件挑前幾名
# （選項太多 Jev 不容易選，使用者改選的清單也太長）；接正式 Jev API 時依它的上限調整
MAX_CHOICE_OPTIONS = 8


def _subject(req: PlanRequest, *extra: str) -> str:
    """選擇題的對象代碼：同樣的條件 → 同樣的代碼，使用者改選時才對得上。"""
    conditions = req.model_dump(mode="json", exclude={"overrides"})
    digest = hashlib.sha1(repr(sorted(conditions.items())).encode("utf-8")).hexdigest()[:12]
    return "|".join([req.area, req.date.isoformat(), digest, *extra])


def _user_text(req: PlanRequest) -> str:
    budget = f"{req.budget_per_person} 元" if req.budget_per_person else "不限"
    return (
        f"想看的片型：{req.genre or '不限'}；飲食限制：{req.diet or '無'}；"
        f"想吃：{'、'.join(req.cuisines) or '不限'}；每人預算（含來回車資）：{budget}；"
        f"指定電影：{req.movie_query or '交給 Jev 決定'}"
    )


def _logistics_key(c: Candidate) -> tuple:
    """同一組片和餐廳有好幾種排法時，程式挑緩衝最充裕、走路最少、最便宜的。"""
    return (min(c.tightest, 30), -c.walk_total, -c.cost, c.tightest)


def _top_movies(req: PlanRequest, by_movie: dict[str, list[Candidate]]) -> list[str]:
    """要給 Jev 選的片，取前 MAX_CHOICE_OPTIONS 部：使用者在改選清單點過的片 → 符合想看的片型 →
    緩衝夠寬裕（超過 30 分鐘都算一樣，和 _logistics_key 相同）→ 排得進去的場次多 → 片名。"""
    chosen = req.overrides.get(f"movie_choice:{_subject(req)}")

    def key(title: str) -> tuple:
        group = by_movie[title]
        slots = {(c.showtime.start, c.showtime.cinema.name, c.showtime.label) for c in group}
        wanted = bool(req.genre) and group[0].showtime.genre == req.genre
        return (title != chosen, not wanted, -min(max(c.tightest for c in group), 30), -len(slots), title)

    return sorted(by_movie, key=key)[:MAX_CHOICE_OPTIONS]


def _top_restaurants(req: PlanRequest, movie: str, by_name: dict[str, list[Candidate]]) -> list[str]:
    """要給 Jev 選的餐廳，取前 MAX_CHOICE_OPTIONS 家：使用者在改選清單點過的店 → Jev 判斷的約會適合度 →
    離影城近 → 店名。（想吃的料理在 _keep_restaurant 已經篩過，到這裡的店都符合。）"""
    chosen = req.overrides.get(f"restaurant_choice:{_subject(req, movie)}")

    def key(name: str) -> tuple:
        best = max(by_name[name], key=_logistics_key)
        r = best.restaurant
        return (name != chosen, -r.date_fit.prob("適合約會"), walk_minutes(best.showtime.cinema, r.place), name)

    return sorted(by_name, key=key)[:MAX_CHOICE_OPTIONS]


def _ask_movie(req: PlanRequest, ctx: PlanContext, by_movie: dict[str, list[Candidate]]) -> JevAnswer:
    titles = sorted(by_movie)
    question = movie_choice_question(titles)
    if len(titles) == 1:  # 只有一部片排得進去（或使用者指定了片），不用問
        source = "user" if req.movie_query else "program"
        return JevAnswer(f"{question.key}:{_subject(req)}", question.text, titles, [1.0], source=source)
    options, features = [], {}
    for title in titles:
        group = by_movie[title]
        s = group[0].showtime
        slots = sorted({(c.showtime.start, c.showtime.cinema.name, c.showtime.label) for c in group})
        slot_text = "、".join(f"{cinema} {fmt(start)}（{label}）" for start, cinema, label in slots[:6])
        best_buffer = max(c.tightest for c in group)
        options.append(
            f"「{title}」：{s.genre or '片型未知'}片，{s.runtime_min} 分鐘。簡介：{s.synopsis or '無'}。"
            f"排得進去的場次：{slot_text}；最寬裕的組合緩衝 {best_buffer} 分鐘。"
        )
        features[title] = {
            "genre": s.genre,
            "premium": any(c.showtime.screen.top in ("IMAX", "4DX") for c in group),
            "best_buffer": best_buffer,
        }
    context = {"user": _user_text(req), "options": options, "_features": features, "_wanted_genre": req.genre}
    return ctx.ask(question, _subject(req), context)


def _ask_restaurant(
    req: PlanRequest, ctx: PlanContext, movie: str, group: list[Candidate]
) -> tuple[JevAnswer, dict[str, list[Candidate]]]:
    by_restaurant: dict[str, list[Candidate]] = defaultdict(list)
    for c in group:
        by_restaurant[c.restaurant.place.name].append(c)
    by_restaurant = {name: by_restaurant[name] for name in _top_restaurants(req, movie, by_restaurant)}
    names = sorted(by_restaurant)
    question = restaurant_choice_question(movie, names)
    if len(names) == 1:
        answer = JevAnswer(f"{question.key}:{_subject(req, movie)}", question.text, names, [1.0], source="program")
        return answer, by_restaurant
    options, features = [], {}
    for name in names:
        best = max(by_restaurant[name], key=_logistics_key)
        r = best.restaurant
        walk = walk_minutes(best.showtime.cinema, r.place)
        order = "先看電影再吃飯" if best.order == "movie_first" else "先吃飯再看電影"
        options.append(
            f"「{name}」：{r.cuisine.top}，每人約 {r.price} 元。介紹：{r.description}。評論：{'；'.join(r.reviews)}。"
            f"搭配這部片：{order}，和影城步行 {walk} 分鐘，最緊緩衝 {best.tightest} 分鐘，"
            f"電影加餐費每人 {best.showtime.price + r.price} 元，來回車資約 {best.go.fare + best.back.fare} 元。"
        )
        features[name] = {
            "date_fit": r.date_fit.prob("適合約會"),
            "date_ok": r.date_fit.prob("普通"),
            "cuisine_match": bool(req.cuisines) and r.cuisine.top in req.cuisines,
            "budget": req.budget_per_person,
            "cost": best.cost,
            "buffer": best.tightest,
            "walk": walk,
        }
    context = {"user": _user_text(req), "movie": movie, "options": options, "_features": features}
    return ctx.ask(question, _subject(req, movie), context), by_restaurant


def _choice_info(answer: JevAnswer, choice: str, rank: int) -> dict:
    return {"question": answer.question, "choice": choice, "prob": round(answer.prob(choice), 3),
            "rank": rank, "source": answer.source}


def _jev_pick(
    req: PlanRequest, ctx: PlanContext, candidates: list[Candidate], asks: dict, notes: list[str]
) -> list[dict]:
    """Jev 選片、選餐廳；組合的信心＝選片機率 × 選餐廳機率，取前三名。選項有縮減時在 notes 說明。"""
    if not candidates:
        return []
    by_movie: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        by_movie[c.showtime.movie_title].append(c)
    titles = _top_movies(req, by_movie)
    if len(titles) < len(by_movie):
        basis = "想看的片型與時間餘裕" if req.genre else "時間餘裕與場次多寡"
        notes.append(f"排得進去的片有 {len(by_movie)} 部，程式先依{basis}挑 {len(titles)} 部讓 Jev 選。")
        by_movie = {title: by_movie[title] for title in titles}

    movie_ans = _ask_movie(req, ctx, by_movie)
    movie_rank = [m for m in sorted(movie_ans.options, key=lambda m: -movie_ans.prob(m)) if movie_ans.prob(m) > 0]
    if len(movie_ans.options) > 1 and not movie_ans.confident(ctx.threshold):
        _ask(asks, movie_ans, "Jev 不太確定要看哪部片，你比較想看哪一部？", 4, kind="choice")

    combos = []
    for rank, movie in enumerate(movie_rank[:TOP_MOVIES], start=1):
        rest_ans, by_restaurant = _ask_restaurant(req, ctx, movie, by_movie[movie])
        total = len({c.restaurant.place.name for c in by_movie[movie]})
        restaurant_note = (
            f"配得上的餐廳超過 {MAX_CHOICE_OPTIONS} 家時，程式先依約會適合度與步行距離挑 {MAX_CHOICE_OPTIONS} 家讓 Jev 選。"
        )
        if len(by_restaurant) < total and restaurant_note not in notes:
            notes.append(restaurant_note)
        if rank == 1 and len(rest_ans.options) > 1 and not rest_ans.confident(ctx.threshold):
            _ask(asks, rest_ans, f"看《{movie}》的話，Jev 不太確定要吃哪一家，你比較想去哪裡？", 5, kind="choice")
        rest_rank = sorted(rest_ans.options, key=lambda n: -rest_ans.prob(n))
        for r_rank, name in enumerate(rest_rank, start=1):
            joint = movie_ans.prob(movie) * rest_ans.prob(name)
            if joint <= 0:
                continue
            combos.append(
                {
                    "joint": joint,
                    "candidate": max(by_restaurant[name], key=_logistics_key),
                    "movie": _choice_info(movie_ans, movie, rank),
                    "restaurant": _choice_info(rest_ans, name, r_rank),
                }
            )
    combos.sort(key=lambda x: -x["joint"])
    return combos[:PLAN_COUNT]


def _transfers(route: Route) -> int:
    return max(len(route.legs) - 1, 0)


def _last_margin(route: Route) -> int | None:
    """這趟車離末班車最近的一段還有幾分鐘；走路就到的沒有末班車。"""
    return min((leg.last - leg.ready for leg in route.legs), default=None)


def _trip_text(o: TripOption, going: bool) -> str:
    r = o.route
    ride = sum(leg.ride_min for leg in r.legs)
    longest_wait = max((leg.wait for leg in r.legs), default=0)
    when = f"{fmt(r.depart)} 出門、{fmt(r.arrive)} 到站" if going else f"{fmt(r.depart)} 開始等車、{fmt(r.arrive)} 到家"
    text = (
        f"「{o.label}」：{when}，車程 {ride} 分鐘（不含等車），轉乘 {_transfers(r)} 次，"
        f"最久等車 {longest_wait} 分鐘，每人車資約 {o.fare} 元（估計）"
    )
    margin = None if going else _last_margin(r)
    if margin is not None:
        text += f"，離末班車還有 {margin} 分鐘"
    return text + "。"


def _trip_user_text(req: PlanRequest, ctx: PlanContext) -> str:
    return (
        f"{_user_text(req)}；從{station_name(ctx.origin.name)}出發，到{req.area}；"
        f"最早 {req.depart_after} 出門，最晚 {req.home_by} 回到出發站"
    )


def _ask_trip(
    req: PlanRequest, ctx: PlanContext, movie: str, restaurant: str, base_cost: int,
    direction: str, options: list[TripOption], budget_left: int, plan_note: str = "",
) -> JevAnswer:
    """問 Jev 這個方案的去程或回程搭哪一種；options 由快到慢。只有一種走法時不問。

    Jev 只看得到文字，所以去程搭了什麼、剩下的車資預算要由 plan_note 寫進方案說明
    （快取的雜湊也是從文字算的，去程改選後回程才會重問）；_budget_left 只給模擬版用。
    """
    labels = [o.label for o in options]
    question = transit_choice_question(direction, labels)
    subject = _subject(req, movie, restaurant)
    if len(options) == 1:
        return JevAnswer(f"{question.key}:{subject}", question.text, labels, [1.0], source="program")
    going = direction == "去程"
    if going:  # 去程比出門時間：可以晚出門幾分鐘
        saved = {o.label: o.route.depart - min(x.route.depart for x in options) for o in options}
    else:  # 回程比到家時間：可以早到家幾分鐘
        saved = {o.label: max(x.route.arrive for x in options) - o.route.arrive for o in options}
    features = {
        o.label: {
            "saved": saved[o.label],
            "transfers": _transfers(o.route),
            "fare": o.fare,
            "last_margin": None if going else _last_margin(o.route),
        }
        for o in options
    }
    context = {
        "user": _trip_user_text(req, ctx),
        "plan": f"看《{movie}》、在{restaurant}吃飯，電影加餐費每人 {base_cost} 元{plan_note}",
        "options": [_trip_text(o, going) for o in options],
        "_features": features,
        "_budget_left": budget_left,
    }
    return ctx.ask(question, subject, context)


def _choose_trips(req: PlanRequest, ctx: PlanContext, picked: list[dict], asks: dict) -> None:
    """前三名決定後，請 Jev 選每個方案的去程、回程走法，再用選定的走法重排該方案（電影、用餐時間不變）。

    去程只列付得起的（加上最便宜的回程不超出預算）；回程只列選好去程之後、剩下的預算付得起的。
    """
    budget = req.budget_per_person
    for rank, pick in enumerate(picked, start=1):
        c = pick["candidate"]
        build = BUILDERS[c.order]
        full = build(ctx, req, c.showtime, c.restaurant, RouteChoice(every=True))
        base = c.showtime.price + c.restaurant.price
        movie, restaurant = c.showtime.movie_title, c.restaurant.place.name

        # 一定有付得起的去程：這個組合已通過 _pick_trips（最快的去程一定留著，每種走法都算時最便宜的回程不會比最快的貴）
        gos = _affordable_go(req, base, full.go_options, full.back_options)
        left = budget - base - min(o.fare for o in full.back_options) if budget else 0
        note = f"；去程車資每人最多 {left} 元（已預留最便宜回程的車資）" if budget else ""
        go_ans = _ask_trip(req, ctx, movie, restaurant, base, "去程", gos, left, note)
        go = next(o for o in gos if o.label == go_ans.top)

        backs = _affordable_back(req, base, go, full.back_options)
        left = budget - base - go.fare if budget else 0
        note = f"；去程搭「{go.label}」，每人約 {go.fare} 元" + (f"；回程車資每人最多 {left} 元" if budget else "")
        back_ans = _ask_trip(req, ctx, movie, restaurant, base, "回程", backs, left, note)

        pick["candidate"] = build(ctx, req, c.showtime, c.restaurant, RouteChoice(True, go.label, back_ans.top))
        pick["go"] = _choice_info(go_ans, go_ans.top, 1)
        pick["back"] = _choice_info(back_ans, back_ans.top, 1)
        pick["trip_options"] = (gos, backs)
        if rank == 1:
            for ans, question, priority in (
                (go_ans, "Jev 不太確定去程要搭哪一種，你比較想怎麼去？", 6),
                (back_ans, "Jev 不太確定回程要搭哪一種，你比較想怎麼回？", 7),
            ):
                if len(ans.options) > 1 and not ans.confident(ctx.threshold):
                    _ask(asks, ans, question, priority, kind="choice")


def _labels(picked: list[dict]) -> list[list[str]]:
    """Jev 的名次，加上程式算出來的客觀標籤（三個方案裡最省、最穩的）。"""
    names = ["Jev 首選", "Jev 第二選擇", "Jev 第三選擇"]
    labels = [[names[i]] for i in range(len(picked))]
    if len(picked) > 1:
        costs = [p["candidate"].cost for p in picked]
        buffers = [p["candidate"].tightest for p in picked]
        labels[costs.index(min(costs))].append("最省")
        labels[buffers.index(max(buffers))].append("最穩")
    return labels


ESTIMATE_NOTES = (
    ("movie", "price", "票價為估計"),
    ("restaurant", "hours", "營業時間為估計，出發前請先確認"),
    ("restaurant", "price", "價位為估計"),
)
FARE_NOTE = "車資為估計"  # 車資一律是估計（app/fares.py）


def _estimate_notes(showtime, restaurant) -> list[str]:
    """哪些數字是估計的（爬蟲抓不到），卡片上要提醒。"""
    sources = {"movie": showtime.estimated, "restaurant": restaurant.estimated}
    return [note for kind, name, note in ESTIMATE_NOTES if name in sources[kind]]


def _option_dict(o: TripOption) -> dict:
    return {
        "label": o.label,
        "depart": fmt(o.route.depart),
        "arrive": fmt(o.route.arrive),
        "ride_min": sum(leg.ride_min for leg in o.route.legs),
        "transfers": _transfers(o.route),
        "fare": o.fare,
    }


def _trip_dict(chosen: TripOption, options: list[TripOption]) -> dict:
    return {"choice": chosen.label, "fare": chosen.fare, "options": [_option_dict(o) for o in options]}


def _plan_dict(labels: list[str], pick: dict) -> dict:
    c = pick["candidate"]
    s, r = c.showtime, c.restaurant
    attributes = (s.title_match, s.screen, s.audio, r.date_fit, r.pace, r.cuisine, r.diet_ok)
    return {
        "label": "、".join(labels),
        "labels": labels,
        "order": c.order,
        "order_text": "先看電影再吃飯" if c.order == "movie_first" else "先吃飯再看電影",
        "jev_confidence": round(pick["joint"], 3),
        "jev_choice": {
            "movie": pick["movie"], "restaurant": pick["restaurant"], "go": pick["go"], "back": pick["back"],
        },
        "leave_home": fmt(c.leave_home),
        "arrive_home": fmt(c.arrive_home),
        "cost_per_person": c.cost,
        "cost_breakdown": {"movie": s.price, "meal": r.price, "go": c.go.fare, "back": c.back.fare},
        "transit": {
            "go": _trip_dict(c.go, pick["trip_options"][0]),
            "back": _trip_dict(c.back, pick["trip_options"][1]),
        },
        "tightest_buffer_min": c.tightest,
        "movie": {
            "title": s.movie_title,
            "raw_title": s.raw_title,
            "genre": s.genre,
            "cinema": s.cinema.name,
            "start": fmt(s.start),
            "end": fmt(c.film_end),
            "label": s.label,
            "screen": s.screen.top,
            "audio": s.audio.top,
            "price": s.price,
            "runtime_min": s.runtime_min,
            "estimated": s.estimated,
        },
        "restaurant": {
            "id": r.place.id,
            "name": r.place.name,
            "cuisine": r.cuisine.top,
            "price": r.price,
            "date_fit": r.date_fit.top,
            "pace": r.pace.top,
            "reservable": r.reservable,
            "estimated": r.estimated,
            "url": r.url,
        },
        "steps": [step.to_dict() for step in c.steps],
        "flags": c.flags,
        "tips": c.tips,
        "estimates": _estimate_notes(s, r) + ([FARE_NOTE] if c.go.fare + c.back.fare else []),
        "jev": [a.to_dict() for a in attributes if a is not None],
    }


OUTSIDE_WINDOW = "場次時間落在出門到回家的時間之外"


def _fits_window(req: PlanRequest, s: ShowtimeInfo) -> bool:
    """開場前到得了影城、散場時還沒超過最晚到家：任何排法的必要條件，先篩掉可以省下大量組合。"""
    arrive_cinema = s.start - PRE_SHOW_MIN
    film_end = s.start + ADS_MIN + s.runtime_min
    return arrive_cinema >= to_min(req.depart_after) and film_end <= to_min(req.home_by)


def plan(req: PlanRequest, ctx: PlanContext) -> dict:
    thr = ctx.threshold
    asks: dict[str, dict] = {}
    showtime_filters: Counter = Counter()
    restaurant_filters: Counter = Counter()
    rejections: Counter = Counter()

    # 1. 程式先用硬條件篩：Jev 的屬性判斷（片名、影廳、語言、料理、飲食）＋時間、交通、預算
    showtimes = [s for s in ctx.showtimes if _keep_showtime(req, s, thr, asks, showtime_filters)]
    restaurants = [r for r in ctx.restaurants if _keep_restaurant(req, r, thr, asks, restaurant_filters)]
    builders = {"any": (_movie_first, _dinner_first), "after": (_movie_first,), "before": (_dinner_first,)}[req.meal_order]

    candidates: list[Candidate] = []
    checked = 0
    for s in showtimes:
        if not _fits_window(req, s):
            skipped = len(restaurants) * len(builders)
            checked += skipped
            if skipped:
                rejections[OUTSIDE_WINDOW] += skipped
            continue
        for r in restaurants:
            checked += len(builders)
            if walk_minutes(s.cinema, r.place) > req.max_walk_min:
                rejections[f"影城到餐廳要走超過 {req.max_walk_min} 分鐘"] += len(builders)
                continue
            for build in builders:
                try:
                    c = build(ctx, req, s, r)
                except Reject as exc:
                    rejections[str(exc)] += 1
                    continue
                candidates.append(c)

    # 2. Jev 從接得起來的組合裡選片、選餐廳
    notes: list[str] = []
    picked = _jev_pick(req, ctx, candidates, asks, notes)
    _choose_trips(req, ctx, picked, asks)  # 選車在選片、選餐廳之後，不影響前三名

    if req.genre and candidates and not any(c.showtime.genre == req.genre for c in candidates):
        notes.append(f"你想看的{req.genre}片在這段時間內排不進去，Jev 改從其他片裡選。")

    return {
        "plans": [_plan_dict(labels, pick) for labels, pick in zip(_labels(picked), picked)],
        "notes": notes,
        "confirmations": sorted(asks.values(), key=lambda a: a["priority"])[:MAX_CONFIRMATIONS],
        "stats": {
            "combinations_checked": checked,
            "feasible": len(candidates),
            "movies_feasible": len({c.showtime.movie_title for c in candidates}),
            "restaurants_feasible": len({c.restaurant.place.name for c in candidates}),
        },
        "filters": {
            "showtimes": {"total": len(ctx.showtimes), "kept": len(showtimes), "reasons": dict(showtime_filters)},
            "restaurants": {"total": len(ctx.restaurants), "kept": len(restaurants), "reasons": dict(restaurant_filters)},
        },
        "rejections": [{"reason": reason, "count": n} for reason, n in rejections.most_common()],
        "assumptions": ASSUMPTIONS,
    }

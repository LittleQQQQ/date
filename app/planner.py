"""排程核心（程式計算）。

把「場次 × 餐廳 × 先吃或後吃」全部列出來，逐段檢查時間接不接得上，再挑出 3 個方案。
這個檔案不碰資料庫、也不直接呼叫 Jev：資料和 Jev 的判斷由 services.py 準備好傳進來，方便單獨測試。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from .geo import walk_minutes
from .jev.questions import DATE_FIT, MEAL_MINUTES, JevAnswer
from .schemas import PlanRequest
from .timeutil import fmt, to_min

PRE_SHOW_MIN = 10  # 開場前幾分鐘要到影城
ADS_MIN = 12  # 開場後的廣告與預告
DINNER_BUFFER_MIN = 10  # 先吃飯時，預留提早離開餐廳的彈性
PEAK_START, PEAK_END = to_min("17:30"), to_min("20:30")
MAX_CONFIRMATIONS = 5

ASSUMPTIONS = {
    "開場前到影城": f"{PRE_SHOW_MIN} 分鐘",
    "廣告與預告": f"{ADS_MIN} 分鐘",
    "先吃飯時預留": f"{DINNER_BUFFER_MIN} 分鐘",
    "用餐時間": "快速 40 分、一般 60 分、慢食 90 分（快慢由 Jev 判斷）",
    "步行時間": "直線距離 × 1.3，每分鐘 75 公尺",
    "等車時間": "以班距估最久要等多久",
    "候位時間": "尖峰（17:30–20:30）週五到週日 ×1.5、離峰 ×0.5；可訂位的餐廳假設已訂位",
}


@dataclass
class Place:
    id: int | str
    name: str
    lat: float
    lng: float


@dataclass
class LinkInfo:
    ride_min: int
    headway_min: int
    first_train: int
    last_train: int


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


@dataclass
class PlanContext:
    origin: Place
    area_stations: list[Place]
    links: dict[tuple[str, str], LinkInfo]
    showtimes: list[ShowtimeInfo]
    restaurants: list[RestaurantInfo]
    weekend: bool
    threshold: float


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
    flags: list[str] = field(default_factory=list)  # 風險
    tips: list[str] = field(default_factory=list)  # 提醒
    score: float = 0.0


class Reject(Exception):
    """這個組合接不起來；訊息就是原因，會統計後顯示給使用者。"""


# ---------- 交通 ----------


def _route(ctx: PlanContext, place: Place, inbound: bool) -> tuple[Place, LinkInfo, int]:
    """找出發站 ↔ 商圈內哪個捷運站最快（車程＋等車＋步行）。"""
    best = None
    for station in ctx.area_stations:
        key = (ctx.origin.id, station.id) if inbound else (station.id, ctx.origin.id)
        link = ctx.links.get(key)
        if link is None:
            continue
        walk = walk_minutes(station, place)
        total = link.ride_min + link.headway_min + walk
        if best is None or total < best[0]:
            best = (total, station, link, walk)
    if best is None:
        raise Reject("缺少出發站到這個商圈的捷運資料")
    return best[1], best[2], best[3]


def _go_out(ctx: PlanContext, req: PlanRequest, place: Place, arrive_by: int, steps: list[Step]) -> tuple[int, int]:
    """往回推：要在 arrive_by 抵達 place，幾點要出門。回傳（出門時間, 步行分鐘）。"""
    station, link, walk = _route(ctx, place, inbound=True)
    off_train = arrive_by - walk
    board = off_train - link.ride_min
    leave = board - link.headway_min
    if leave < to_min(req.depart_after):
        raise Reject("要比你能出門的時間更早出發")
    if not link.first_train <= board <= link.last_train:
        raise Reject("去程沒有捷運班次")
    steps.append(
        Step(
            leave,
            off_train,
            "transit",
            f"從{ctx.origin.name}站出發",
            f"搭捷運到{station.name}站，車程 {link.ride_min} 分鐘，預留等車 {link.headway_min} 分鐘",
        )
    )
    steps.append(Step(off_train, arrive_by, "walk", f"步行到{place.name}", f"約 {walk} 分鐘"))
    return leave, walk


def _go_home(
    ctx: PlanContext, req: PlanRequest, place: Place, ready: int, steps: list[Step], flags: list[str]
) -> tuple[int, int]:
    """往後推：ready 時從 place 出發回家，幾點到家。回傳（到家時間, 步行分鐘）。"""
    station, link, walk = _route(ctx, place, inbound=False)
    at_station = ready + walk
    if at_station > link.last_train:
        raise Reject("回程趕不上末班車")
    board = min(at_station + link.headway_min, link.last_train)
    arrive = board + link.ride_min
    if arrive > to_min(req.home_by):
        raise Reject(f"沒辦法在 {fmt(to_min(req.home_by))} 前到家")
    steps.append(Step(ready, at_station, "walk", f"步行到{station.name}站", f"約 {walk} 分鐘"))
    steps.append(
        Step(
            at_station,
            arrive,
            "transit",
            f"搭捷運回{ctx.origin.name}站",
            f"車程 {link.ride_min} 分鐘，等車最久 {board - at_station} 分鐘",
        )
    )
    if link.last_train - at_station < 15:
        flags.append(f"回程接近末班車（{fmt(link.last_train)}）")
    return arrive, walk


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


def _movie_first(ctx: PlanContext, req: PlanRequest, s: ShowtimeInfo, r: RestaurantInfo) -> Candidate:
    steps: list[Step] = []
    flags: list[str] = []
    arrive_cinema = s.start - PRE_SHOW_MIN
    leave, walk_in = _go_out(ctx, req, s.cinema, arrive_cinema, steps)
    film_end = s.start + ADS_MIN + s.runtime_min
    steps += _movie_steps(s, arrive_cinema, film_end)

    walk_cr = walk_minutes(s.cinema, r.place)
    arrive_r = film_end + walk_cr
    steps.append(Step(film_end, arrive_r, "walk", f"步行到{r.place.name}", f"約 {walk_cr} 分鐘"))
    ready = max(arrive_r, r.open)
    if ready > arrive_r:
        steps.append(Step(arrive_r, ready, "wait", "等餐廳開門"))
    wait = _wait(ctx, r, ready)
    seat = ready + wait
    if wait:
        steps.append(Step(ready, seat, "wait", "候位", f"預估 {wait} 分鐘"))
    last_order = r.close - r.last_order_min
    if seat > last_order:
        raise Reject("散場後趕不上餐廳最後點餐")
    meal_end = seat + MEAL_MINUTES[r.pace.top]
    if meal_end > r.close:
        raise Reject("用餐時間會超過餐廳打烊")
    steps.append(_meal_step(r, seat, meal_end, last_order))

    arrive_home, walk_out = _go_home(ctx, req, r.place, meal_end, steps, flags)
    order_slack = last_order - seat
    if order_slack < 15:
        flags.append(f"散場後到最後點餐只剩 {order_slack} 分鐘，候位久一點就吃不到")
    home_slack = to_min(req.home_by) - arrive_home
    return _finish(
        ctx, req, "movie_first", s, r, steps, flags, leave, arrive_home, film_end,
        walk_in + walk_cr + walk_out, wait, min(order_slack, home_slack),
    )


def _dinner_first(ctx: PlanContext, req: PlanRequest, s: ShowtimeInfo, r: RestaurantInfo) -> Candidate:
    steps: list[Step] = []
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

    leave, walk_in = _go_out(ctx, req, r.place, arrive_r, steps)
    if wait:
        steps.append(Step(arrive_r, seat, "wait", "候位", f"預估 {wait} 分鐘"))
    steps.append(_meal_step(r, seat, meal_end, last_order))
    steps.append(Step(meal_end, leave_r, "buffer", "預留彈性時間", f"上菜慢或結帳排隊都還來得及（{DINNER_BUFFER_MIN} 分鐘）"))
    steps.append(Step(leave_r, arrive_cinema, "walk", f"步行到{s.cinema.name}", f"約 {walk_rc} 分鐘"))
    film_end = s.start + ADS_MIN + s.runtime_min
    steps += _movie_steps(s, arrive_cinema, film_end)

    arrive_home, walk_out = _go_home(ctx, req, s.cinema, film_end, steps, flags)
    connection = PRE_SHOW_MIN + DINNER_BUFFER_MIN
    if wait >= 15:
        flags.append(f"候位預估 {wait} 分鐘；比預估多等超過 {connection} 分鐘就會趕不上開場")
    home_slack = to_min(req.home_by) - arrive_home
    return _finish(
        ctx, req, "dinner_first", s, r, steps, flags, leave, arrive_home, film_end,
        walk_in + walk_rc + walk_out, wait, min(connection, home_slack),
    )


def _finish(
    ctx: PlanContext, req: PlanRequest, order: str, s: ShowtimeInfo, r: RestaurantInfo, steps: list[Step],
    flags: list[str], leave: int, arrive_home: int, film_end: int, walk_total: int, wait: int, tightest: int,
) -> Candidate:
    home_by = to_min(req.home_by)
    home_slack = home_by - arrive_home
    if home_slack < 15:
        flags.append(f"只比 {fmt(home_by)} 早 {home_slack} 分鐘到家")
    tips = []
    if r.reservable:
        tips.append("記得先訂位（已假設不用候位）")
    elif wait >= 20:
        flags.append(f"尖峰時段，候位預估 {wait} 分鐘")
    steps.append(Step(arrive_home, None, "arrive", f"回到{ctx.origin.name}站", f"比 {fmt(home_by)} 早 {home_slack} 分鐘"))
    return Candidate(
        order=order, showtime=s, restaurant=r, steps=steps, leave_home=leave, arrive_home=arrive_home,
        film_end=film_end, cost=s.price + r.price, tightest=tightest, walk_total=walk_total, wait=wait,
        flags=flags, tips=tips,
    )


# ---------- 篩選、評分、挑選 ----------


def _ask(asks: dict[str, dict], ans: JevAnswer, question: str, priority: int) -> None:
    if ans.key not in asks:
        asks[ans.key] = {
            "key": ans.key,
            "question": question,
            "options": ans.options,
            "probs": [round(p, 3) for p in ans.probs],
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
        filtered["Jev 判斷不適合約會"] += 1
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
    return True


def _score(req: PlanRequest, c: Candidate) -> float:
    r = c.restaurant
    score = 40 * r.date_fit.prob("適合約會") + 15 * r.date_fit.prob("普通")
    if req.cuisines:
        score += 15 * sum(r.cuisine.prob(x) for x in req.cuisines)
    score += 20 * min(max(c.tightest, 0), 30) / 30
    score -= 0.5 * c.walk_total + 0.3 * c.wait
    if req.budget_per_person:
        score += 10 * (1 - c.cost / req.budget_per_person)
    else:
        score -= c.cost / 200
    return round(score, 1)


def _pick(candidates: list[Candidate]) -> list[tuple[str, Candidate]]:
    """依三種角度各挑一個，盡量挑不同餐廳，讓使用者真的有得選。"""
    rules: list[tuple[str, Callable[[Candidate], object]]] = [
        ("最推薦", lambda c: c.score),
        ("最穩", lambda c: (c.tightest, c.score)),
        ("最省", lambda c: (-c.cost, c.score)),
    ]
    unsuitable = DATE_FIT.options[-1]
    picked: list[tuple[str, Candidate]] = []
    for label, key in rules:
        used = {c.restaurant.place.id for _, c in picked}
        fresh = [c for c in candidates if c.restaurant.place.id not in used]
        # 優先挑 Jev 沒有傾向「不適合約會」的餐廳；真的沒有才退而求其次
        pool = (
            [c for c in fresh if c.restaurant.date_fit.top != unsuitable]
            or fresh
            or [c for c in candidates if all(c is not p for _, p in picked)]
        )
        if pool:
            picked.append((label, max(pool, key=key)))
    return picked


def _plan_dict(label: str, c: Candidate) -> dict:
    s, r = c.showtime, c.restaurant
    return {
        "label": label,
        "order": c.order,
        "order_text": "先看電影再吃飯" if c.order == "movie_first" else "先吃飯再看電影",
        "score": c.score,
        "leave_home": fmt(c.leave_home),
        "arrive_home": fmt(c.arrive_home),
        "cost_per_person": c.cost,
        "tightest_buffer_min": c.tightest,
        "movie": {
            "title": s.movie_title,
            "raw_title": s.raw_title,
            "cinema": s.cinema.name,
            "start": fmt(s.start),
            "end": fmt(c.film_end),
            "label": s.label,
            "screen": s.screen.top,
            "audio": s.audio.top,
            "price": s.price,
            "runtime_min": s.runtime_min,
        },
        "restaurant": {
            "id": r.place.id,
            "name": r.place.name,
            "cuisine": r.cuisine.top,
            "price": r.price,
            "date_fit": r.date_fit.top,
            "pace": r.pace.top,
            "reservable": r.reservable,
        },
        "steps": [step.to_dict() for step in c.steps],
        "flags": c.flags,
        "tips": c.tips,
        "jev": [a.to_dict() for a in (s.title_match, s.screen, s.audio, r.date_fit, r.pace, r.cuisine) if a is not None],
    }


def plan(req: PlanRequest, ctx: PlanContext) -> dict:
    thr = ctx.threshold
    asks: dict[str, dict] = {}
    showtime_filters: Counter = Counter()
    restaurant_filters: Counter = Counter()
    rejections: Counter = Counter()

    showtimes = [s for s in ctx.showtimes if _keep_showtime(req, s, thr, asks, showtime_filters)]
    restaurants = [r for r in ctx.restaurants if _keep_restaurant(req, r, thr, asks, restaurant_filters)]
    builders = {"any": (_movie_first, _dinner_first), "after": (_movie_first,), "before": (_dinner_first,)}[req.meal_order]

    candidates: list[Candidate] = []
    checked = 0
    for s in showtimes:
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
                if req.budget_per_person and c.cost > req.budget_per_person:
                    rejections["超過每人預算"] += 1
                    continue
                c.score = _score(req, c)
                candidates.append(c)

    picked = _pick(candidates)
    for _, c in picked:
        fit = c.restaurant.date_fit
        if not fit.confident(thr):
            _ask(asks, fit, f"「{c.restaurant.place.name}」適合約會嗎？", 3)

    return {
        "plans": [_plan_dict(label, c) for label, c in picked],
        "confirmations": sorted(asks.values(), key=lambda a: a["priority"])[:MAX_CONFIRMATIONS],
        "stats": {"combinations_checked": checked, "feasible": len(candidates)},
        "filters": {
            "showtimes": {"total": len(ctx.showtimes), "kept": len(showtimes), "reasons": dict(showtime_filters)},
            "restaurants": {"total": len(ctx.restaurants), "kept": len(restaurants), "reasons": dict(restaurant_filters)},
        },
        "rejections": [{"reason": reason, "count": n} for reason, n in rejections.most_common()],
        "assumptions": ASSUMPTIONS,
    }

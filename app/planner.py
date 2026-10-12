"""排程核心（程式計算）。

把「場次 × 餐廳 × 先吃或後吃」全部列出來，逐段檢查時間接不接得上，再挑出 3 個方案。
這個檔案不碰資料庫、也不直接呼叫 Jev：資料和 Jev 的判斷由 services.py 準備好傳進來，方便單獨測試。
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from .geo import walk_minutes
from .jev.questions import (
    DATE_FIT,
    MEAL_MINUTES,
    JevAnswer,
    Question,
    movie_choice_question,
    restaurant_choice_question,
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
    "怎麼選": "程式先篩出時間、交通、預算都接得起來的組合；看哪部片、吃哪一家由 Jev 選；同一組片和餐廳有好幾個場次時，程式挑緩衝最充裕的",
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


def _transit_steps(route: Route, departing: bool) -> list[Step]:
    steps = []
    for i, leg in enumerate(route.legs):
        title = f"搭{leg.label}到{station_name(leg.to_name)}"
        if departing and i == 0:
            title = f"從{station_name(leg.from_name)}出發，{title}"
        detail = f"車程 {leg.ride_min} 分鐘，等車最久 {leg.wait} 分鐘"
        if leg.transfer_min:
            detail = f"在{station_name(leg.from_name)}轉乘（步行 {leg.transfer_min} 分鐘）；{detail}"
        steps.append(Step(leg.ready - leg.transfer_min, leg.alight, "transit", title, detail))
    return steps


def _go_out(ctx: PlanContext, req: PlanRequest, place: Place, arrive_by: int, steps: list[Step]) -> tuple[int, int]:
    """往回推：要在 arrive_by 抵達 place，最晚幾點要出門。回傳（出門時間, 步行分鐘）。"""
    best: tuple[Route, Place, int] | None = None
    for station, walk in _nearby_stations(ctx, place):
        route = ctx.graph.latest_departure(ctx.origin.id, station.id, arrive_by - walk)
        if route is not None and (best is None or route.depart > best[0].depart):
            best = (route, station, walk)
    if best is None:
        raise Reject("去程沒有可以搭的班次")
    route, station, walk = best
    if route.depart < to_min(req.depart_after):
        raise Reject("要比你能出門的時間更早出發")

    steps += _transit_steps(route, departing=True)
    title = f"步行到{place.name}" if route.legs else f"從{station_name(ctx.origin.name)}步行到{place.name}"
    at_place = route.arrive + walk
    steps.append(Step(route.arrive, at_place, "walk", title, f"約 {walk} 分鐘"))
    if at_place < arrive_by:  # 等車以最壞情況估，通常會提早到
        steps.append(Step(at_place, arrive_by, "buffer", "提早抵達", f"多出 {arrive_by - at_place} 分鐘空檔"))
    return route.depart, walk


def _go_home(
    ctx: PlanContext, req: PlanRequest, place: Place, ready: int, steps: list[Step], flags: list[str]
) -> tuple[int, int]:
    """往後推：ready 時從 place 出發回家，最早幾點到家。回傳（到家時間, 步行分鐘）。"""
    best: tuple[Route, Place, int] | None = None
    for station, walk in _nearby_stations(ctx, place):
        route = ctx.graph.earliest_arrival(station.id, ready + walk, ctx.origin.id)
        if route is not None and (best is None or route.arrive < best[0].arrive):
            best = (route, station, walk)
    if best is None:
        raise Reject("回程趕不上末班車")
    route, station, walk = best
    home_by = to_min(req.home_by)
    if route.arrive > home_by:
        raise Reject(f"沒辦法在 {fmt(home_by)} 前到家")

    if route.legs:
        steps.append(Step(ready, ready + walk, "walk", f"步行到{station_name(station.name)}", f"約 {walk} 分鐘"))
        steps += _transit_steps(route, departing=False)
        for leg in route.legs:
            if leg.last - leg.ready < 15:
                flags.append(f"{leg.label}接近末班車（{station_name(leg.from_name)} {fmt(leg.last)} 發車）")
    else:
        steps.append(Step(ready, ready + walk, "walk", f"步行回{station_name(ctx.origin.name)}", f"約 {walk} 分鐘"))
    return route.arrive, walk


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
    steps.append(Step(arrive_home, None, "arrive", f"回到{station_name(ctx.origin.name)}", f"比 {fmt(home_by)} 早 {home_slack} 分鐘"))
    return Candidate(
        order=order, showtime=s, restaurant=r, steps=steps, leave_home=leave, arrive_home=arrive_home,
        film_end=film_end, cost=s.price + r.price, tightest=tightest, walk_total=walk_total, wait=wait,
        flags=flags, tips=tips,
    )


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


def _subject(req: PlanRequest, *extra: str) -> str:
    """選擇題的對象代碼：同樣的條件 → 同樣的代碼，使用者改選時才對得上。"""
    conditions = req.model_dump(mode="json", exclude={"overrides"})
    digest = hashlib.sha1(repr(sorted(conditions.items())).encode("utf-8")).hexdigest()[:12]
    return "|".join([req.area, req.date.isoformat(), digest, *extra])


def _user_text(req: PlanRequest) -> str:
    budget = f"{req.budget_per_person} 元" if req.budget_per_person else "不限"
    return (
        f"想看的片型：{req.genre or '不限'}；飲食限制：{req.diet or '無'}；"
        f"想吃：{'、'.join(req.cuisines) or '不限'}；每人預算：{budget}；"
        f"指定電影：{req.movie_query or '交給 Jev 決定'}"
    )


def _logistics_key(c: Candidate) -> tuple:
    """同一組片和餐廳有好幾種排法時，程式挑緩衝最充裕、走路最少、最便宜的。"""
    return (min(c.tightest, 30), -c.walk_total, -c.cost, c.tightest)


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
            f"搭配這部片：{order}，和影城步行 {walk} 分鐘，最緊緩衝 {best.tightest} 分鐘，電影加餐費每人 {best.cost} 元。"
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


def _jev_pick(req: PlanRequest, ctx: PlanContext, candidates: list[Candidate], asks: dict) -> list[dict]:
    """Jev 選片、選餐廳；組合的信心＝選片機率 × 選餐廳機率，取前三名。"""
    if not candidates:
        return []
    by_movie: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        by_movie[c.showtime.movie_title].append(c)

    movie_ans = _ask_movie(req, ctx, by_movie)
    movie_rank = [m for m in sorted(movie_ans.options, key=lambda m: -movie_ans.prob(m)) if movie_ans.prob(m) > 0]
    if len(movie_ans.options) > 1 and not movie_ans.confident(ctx.threshold):
        _ask(asks, movie_ans, "Jev 不太確定要看哪部片，你比較想看哪一部？", 4, kind="choice")

    combos = []
    for rank, movie in enumerate(movie_rank[:TOP_MOVIES], start=1):
        rest_ans, by_restaurant = _ask_restaurant(req, ctx, movie, by_movie[movie])
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


def _estimate_notes(showtime, restaurant) -> list[str]:
    """哪些數字是估計的（爬蟲抓不到），卡片上要提醒。"""
    sources = {"movie": showtime.estimated, "restaurant": restaurant.estimated}
    return [note for kind, name, note in ESTIMATE_NOTES if name in sources[kind]]


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
        "jev_choice": {"movie": pick["movie"], "restaurant": pick["restaurant"]},
        "leave_home": fmt(c.leave_home),
        "arrive_home": fmt(c.arrive_home),
        "cost_per_person": c.cost,
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
        },
        "steps": [step.to_dict() for step in c.steps],
        "flags": c.flags,
        "tips": c.tips,
        "estimates": _estimate_notes(s, r),
        "jev": [a.to_dict() for a in attributes if a is not None],
    }


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
                candidates.append(c)

    # 2. Jev 從接得起來的組合裡選片、選餐廳
    picked = _jev_pick(req, ctx, candidates, asks)

    notes = []
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

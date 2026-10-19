"""規劃（規格書 7.1）：組候選 → 算交通 → 問 Jev → 排序。在 RQ worker 裡執行，不碰資料庫（資料由 services.py 準備）。

1. 組候選：依使用者選的順序，把「場次 × 餐廳」排成時間軸：
   - 先看電影：出發地 →（開演前 15 分鐘到）影城 → 電影 → 餐廳（用餐 90 分鐘）→ 終點
   - 先吃飯：出發地 → 餐廳（用餐 90 分鐘）→（開演前 15 分鐘到）影城 → 電影 → 終點
   排除接不上的：趕不上開演、用餐時段不在營業時間內、回程沒有車。
2. 依「總空檔時間最短」取前 5 個【預設】。空檔＝從出發時間到回到終點，扣掉看電影、吃飯、路上的時間。
   同一個場次、同一家餐廳盡量不重複（先挑都沒出現過的，不夠再補），免得 5 個候選都是同一場電影。
3. 每個候選問一次 Jev：time_fit、transit_ease（打分數）、budget_fit（選一個）。信心不足改問 LLM。
4. 分數 ＝（time_fit ＋ transit_ease ＋ P(在預算內) ＋ 0.5 × P(接近上限)）÷ 3【預設權重】，高的排前面；
   Jev 和 LLM 都沒結果的候選排到最後。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from .jev.client import Answer
from .jev.questions import PLAN_QUESTIONS
from .jev.service import get_jev, judge
from .routing import Place, Router, Trip
from .timeutil import TW, hhmm

ARRIVE_BEFORE_SHOW = 15  # 開演前幾分鐘到影城【預設】
MEAL_MIN = 90  # 用餐時間【預設】
CANDIDATES = 5  # 取幾個候選問 Jev【預設】
# 一次規劃最多查幾家 Google 餐廳的店家資料（Enterprise 每月 950 次，平均一次規劃 5 次約 190 次規劃）【預設】
MAX_DETAILS_PER_PLAN = CANDIDATES * 2

Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class ShowInfo:
    id: int
    cinema: Place
    title: str
    version: str
    start: int  # 開演（規劃那天 00:00 起的分鐘數）
    end: int  # 散場
    ticket_price: int  # 每人


@dataclass(frozen=True)
class Eatery:
    id: int
    place: Place
    source: str  # osm 或 google
    category_code: str | None
    open: int | None  # 那天的營業時段；None 是公休
    close: int | None
    hours_text: str  # 顯示用，例如「11:00–21:00（估計）」
    hours_estimated: bool
    price_per_person: int  # 估計
    price_level: str  # INEXPENSIVE、MODERATE、EXPENSIVE、VERY_EXPENSIVE
    address: str = ""
    external_id: str = ""  # Google 的 place_id（補店家資料用）
    enriched: bool = False  # Google 餐廳是不是已經補過店家資料


@dataclass
class PlanContext:
    day: dt.date
    depart: int  # 出發時間（分鐘）
    origin: Place
    destination: Place
    order: str  # movie_first 或 meal_first
    party_size: int
    budget_total: int
    showtimes: list[ShowInfo]
    eateries: list[Eatery]
    router: Router
    restaurant_source: str = "osm"
    # Google 餐廳要補店家資料（Place Details）；查不到回傳 None，這個候選就不用
    enrich: Callable[[Eatery], Eatery | None] | None = None


@dataclass
class Candidate:
    show: ShowInfo
    eatery: Eatery
    go: Trip  # 出發地 → 第一站
    middle: Trip  # 第一站 → 第二站
    back: Trip  # 第二站 → 終點
    arrive_cinema: int
    meal_start: int
    meal_end: int
    idle: int  # 總空檔
    answers: dict[str, Answer | None] = field(default_factory=dict)
    score: float | None = None

    @property
    def trips(self) -> list[Trip]:
        return [self.go, self.middle, self.back]

    @property
    def end(self) -> int:
        return self.back.arrive

    @property
    def buffer(self) -> int:
        """開演前提早幾分鐘到（最緊的一段）。"""
        return self.show.start - self.arrive_cinema

    @property
    def transit_total(self) -> int:
        return sum(t.fare for t in self.trips)


class Reject(Exception):
    """這個組合接不上。"""


# ---------- 組候選 ----------


def _movie_first(ctx: PlanContext, s: ShowInfo, e: Eatery) -> Candidate:
    go = ctx.router.arrive_by(ctx.origin, s.cinema, s.start - ARRIVE_BEFORE_SHOW, ctx.depart)
    if go is None:
        raise Reject("去程趕不上開演")
    middle = ctx.router.depart_after(s.cinema, e.place, s.end)
    if middle is None:
        raise Reject("散場後到不了餐廳")
    meal_start = max(middle.arrive, e.open)
    meal_end = meal_start + MEAL_MIN
    if meal_end > e.close:
        raise Reject("用餐時段不在營業時間內")
    back = ctx.router.depart_after(e.place, ctx.destination, meal_end)
    if back is None:
        raise Reject("回程沒有車")
    return _candidate(ctx, s, e, go, middle, back, go.arrive, meal_start, meal_end)


def _meal_first(ctx: PlanContext, s: ShowInfo, e: Eatery) -> Candidate:
    arrive_cinema = s.start - ARRIVE_BEFORE_SHOW
    middle = ctx.router.arrive_by(e.place, s.cinema, arrive_cinema, ctx.depart)
    if middle is None:
        raise Reject("吃完飯趕不上開演")
    meal_end = min(middle.depart, e.close)  # 打烊得早就早點離開，到影城等
    meal_start = meal_end - MEAL_MIN
    if meal_start < e.open:
        raise Reject("用餐時段不在營業時間內")
    go = ctx.router.arrive_by(ctx.origin, e.place, meal_start, ctx.depart)
    if go is None:
        raise Reject("去程趕不上用餐")
    back = ctx.router.depart_after(s.cinema, ctx.destination, s.end)
    if back is None:
        raise Reject("回程沒有車")
    return _candidate(ctx, s, e, go, middle, back, middle.arrive, meal_start, meal_end)


def _candidate(ctx: PlanContext, s: ShowInfo, e: Eatery, go: Trip, middle: Trip, back: Trip,
               arrive_cinema: int, meal_start: int, meal_end: int) -> Candidate:
    busy = (s.end - s.start) + MEAL_MIN + sum(t.arrive - t.depart for t in (go, middle, back))
    idle = back.arrive - ctx.depart - busy
    return Candidate(s, e, go, middle, back, arrive_cinema, meal_start, meal_end, idle)


def build_candidates(ctx: PlanContext, progress: Progress | None = None) -> tuple[list[Candidate], dict[str, int]]:
    """所有接得上的組合裡，取總空檔最短的 CANDIDATES 個。回傳（候選, 排除原因的統計）。"""
    build = _movie_first if ctx.order == "movie_first" else _meal_first
    found: list[Candidate] = []
    rejected: dict[str, int] = {}
    eateries = [e for e in ctx.eateries if e.open is not None]
    total = len(ctx.showtimes)
    for i, s in enumerate(ctx.showtimes):
        if progress:
            progress("routing", i, total)
        for e in eateries:
            try:
                found.append(build(ctx, s, e))
            except Reject as exc:
                rejected[str(exc)] = rejected.get(str(exc), 0) + 1
    found.sort(key=lambda c: (c.idle, c.end, c.show.id, c.eatery.id))
    return _pick_varied(found, _acceptor(ctx, build, progress)), rejected


def _acceptor(ctx: PlanContext, build, progress: Progress | None) -> Callable[[Candidate], Candidate | None]:
    """挑候選時檢查一次：Google 餐廳先補店家資料（規格書 7.1 第 4 步），用真的營業時間重新排；接不上就不用。"""
    details: dict[int, Eatery | None] = {}

    def accept(c: Candidate) -> Candidate | None:
        e = c.eatery
        if e.source != "google" or e.enriched:
            return c
        if ctx.enrich is None:
            return None
        if e.id not in details:
            if len(details) >= MAX_DETAILS_PER_PLAN:  # 查太多家都接不上：剩下的 Google 餐廳不查了，省額度
                return None
            if progress:
                progress("fetching_places", len(details), CANDIDATES)
            details[e.id] = ctx.enrich(e)
        enriched = details[e.id]
        if enriched is None or enriched.open is None:
            return None
        try:
            return build(ctx, c.show, enriched)
        except Reject:
            return None

    return accept


def _pick_varied(found: list[Candidate], accept: Callable[[Candidate], Candidate | None]) -> list[Candidate]:
    picked: list[Candidate] = []
    used: set[tuple[int, int]] = set()
    shows, eateries = set(), set()
    for distinct in (True, False):  # 先挑場次、餐廳都還沒出現過的，不夠再依空檔補
        for c in found:
            if len(picked) == CANDIDATES:
                return picked
            key = (c.show.id, c.eatery.id)
            if key in used or (distinct and (c.show.id in shows or c.eatery.id in eateries)):
                continue
            used.add(key)
            if (ok := accept(c)) is None:
                continue
            picked.append(ok)
            shows.add(c.show.id)
            eateries.add(c.eatery.id)
    return picked


# ---------- 問 Jev ----------


def _clock(minutes: int) -> str:
    return hhmm(minutes % (24 * 60)) if minutes < 24 * 60 else f"隔天 {hhmm(minutes - 24 * 60)}"


def _trip_line(trip: Trip) -> str:
    if trip.is_walk:
        return f"{_clock(trip.depart)}–{_clock(trip.arrive)} 從{trip.from_name}步行到{trip.to_name}（約 {trip.walk_min} 分鐘）"
    fare = f"每人車資{'約' if trip.fare_estimated else ''} {trip.fare} 元"
    return (f"{_clock(trip.depart)}–{_clock(trip.arrive)} 從{trip.from_name}到{trip.to_name}：{trip.summary}"
            f"（轉乘 {trip.transfers} 次、等車最久 {trip.longest_wait} 分鐘、{fare}）")


def plan_state(ctx: PlanContext, c: Candidate) -> str:
    """給 Jev 判斷的文字：時間軸、各項總額（票價、車資乘以人數）、預算、價位、營業時間。"""
    s, e, n = c.show, c.eatery, ctx.party_size
    steps = [_trip_line(c.go)]
    movie = f"{_clock(s.start)}–{_clock(s.end)} 看電影《{s.title}》（{s.version}）於{s.cinema.name}，開演前 {c.buffer} 分鐘到"
    meal = f"{_clock(c.meal_start)}–{_clock(c.meal_end)} 在{e.place.name}用餐（營業時間：{e.hours_text}）"
    steps += [movie, _trip_line(c.middle), meal] if ctx.order == "movie_first" else [meal, _trip_line(c.middle), movie]
    steps.append(_trip_line(c.back))
    tickets = s.ticket_price * n
    transit = c.transit_total * n
    meal_total = e.price_per_person * n
    walk = sum(t.walk_min for t in c.trips)
    transfers = sum(t.transfers for t in c.trips)
    longest_wait = max(t.longest_wait for t in c.trips)
    return "\n".join([
        f"人數：{n} 人",
        f"總預算：{ctx.budget_total} 元",
        "時間軸：",
        *steps,
        f"最緊的緩衝：{c.buffer} 分鐘",
        f"轉乘：{transfers} 次",
        f"步行合計：{walk} 分鐘",
        f"等車最久：{longest_wait} 分鐘",
        f"電影票：{s.ticket_price} 元 × {n} 人 = {tickets} 元",
        f"車資：{c.transit_total} 元 × {n} 人 = {transit} 元",
        f"餐廳價位：{e.price_level}（估計每人 {e.price_per_person} 元，{n} 人 {meal_total} 元）",
        f"總花費估計：{tickets + transit + meal_total} 元",
    ])


def score(answers: dict[str, Answer | None]) -> float | None:
    """（time_fit ＋ transit_ease ＋ P(within) ＋ 0.5 × P(borderline)）÷ 3；有一題沒結果就是 None。"""
    time_fit, transit, budget = answers.get("time_fit"), answers.get("transit_ease"), answers.get("budget_fit")
    if time_fit is None or transit is None or budget is None or time_fit.score is None or transit.score is None:
        return None
    probs = budget.probabilities
    return (time_fit.score + transit.score + probs.get("within", 0.0) + 0.5 * probs.get("borderline", 0.0)) / 3


def judge_candidates(ctx: PlanContext, candidates: list[Candidate], jev=None, progress: Progress | None = None) -> list[Candidate]:
    jev = jev or get_jev()
    for i, c in enumerate(candidates):
        if progress:
            progress("judging", i, len(candidates))
            if hasattr(jev, "on_wait"):  # 正式版碰到每分鐘上限、排隊等的時候
                jev.on_wait = lambda _seconds, i=i: progress("waiting_quota", i, len(candidates))
        c.answers = judge(plan_state(ctx, c), list(PLAN_QUESTIONS), jev,
                          on_fallback=(lambda: progress("llm_fallback", i, len(candidates))) if progress else None)
        c.score = score(c.answers)
    # 分數高的排前面；Jev 和 LLM 都沒結果的排到最後
    return sorted(candidates, key=lambda c: (c.score is None, -(c.score or 0.0), c.idle))


# ---------- 結果格式（規格書 6.5） ----------


def at(day: dt.date, minutes: int) -> str:
    return (dt.datetime(day.year, day.month, day.day, tzinfo=TW) + dt.timedelta(minutes=minutes)).isoformat()


def _trip_item(day: dt.date, trip: Trip) -> dict:
    item = {
        "type": "transit", "from": trip.from_name, "to": trip.to_name,
        "depart_at": at(day, trip.depart), "arrive_at": at(day, trip.arrive), "minutes": trip.arrive - trip.depart,
        "walk_minutes": trip.walk_min,
    }
    if trip.is_walk:
        return item | {"summary": trip.summary, "source": "walk_estimate"}
    return item | {
        "summary": trip.summary, "source": "network", "fare_per_person": trip.fare, "fare_estimated": trip.fare_estimated,
        "rides": [
            {"line": r.line, "from": r.from_name, "to": r.to_name, "board_at": at(day, r.board), "alight_at": at(day, r.alight),
             "wait_max_minutes": r.wait, "fare_per_person": r.fare, "fare_estimated": r.fare_estimated}
            for r in trip.rides
        ],
    }


def _decided_by(answers: dict[str, Answer | None]) -> str | None:
    found = {a.decided_by for a in answers.values() if a is not None}
    return None if not found else ("llm" if "llm" in found else "jev")


def plan_dict(ctx: PlanContext, c: Candidate, rank: int) -> dict:
    s, e, n, day = c.show, c.eatery, ctx.party_size, ctx.day
    movie = {
        "type": "movie", "cinema": s.cinema.name, "title": s.title, "version": s.version,
        "arrive_at": at(day, c.arrive_cinema), "starts_at": at(day, s.start), "ends_at": at(day, s.end),
        "ticket_price_per_person": s.ticket_price,
    }
    meal = {
        "type": "meal", "name": e.place.name, "address": e.address, "source": e.source,
        "attribution": "Google Maps" if e.source == "google" else "© OpenStreetMap 貢獻者",
        "opening_hours_today": e.hours_text, "hours_estimated": e.hours_estimated,
        "price_level": e.price_level, "price_per_person_estimate": e.price_per_person,
        "arrive_at": at(day, c.middle.arrive if ctx.order == "movie_first" else c.go.arrive),
        "seat_at": at(day, c.meal_start), "leave_at": at(day, c.meal_end),
    }
    middle = [movie, _trip_item(day, c.middle), meal] if ctx.order == "movie_first" else [meal, _trip_item(day, c.middle), movie]
    budget = c.answers.get("budget_fit")
    return {
        "rank": rank,
        "score": None if c.score is None else round(c.score, 3),
        "decided_by": _decided_by(c.answers),
        "timeline": [_trip_item(day, c.go), *middle, _trip_item(day, c.back)],
        "depart_at": at(day, c.go.depart),
        "arrive_at": at(day, c.end),
        "idle_minutes": c.idle,
        "cost": {
            "tickets_total": s.ticket_price * n,
            "transit_total": c.transit_total * n,
            "transit_estimated": any(t.fare_estimated for t in c.trips),
            "meal_total_estimate": e.price_per_person * n,
            "price_level": e.price_level,
            "total_estimate": (s.ticket_price + c.transit_total + e.price_per_person) * n,
        },
        "judgment": {
            "time_fit": _score_of(c.answers.get("time_fit")),
            "transit_ease": _score_of(c.answers.get("transit_ease")),
            "budget_within_prob": None if budget is None else round(budget.probabilities.get("within", 0.0), 3),
            "budget_fit": None if budget is None else budget.choice,
        },
    }


def _score_of(answer: Answer | None) -> float | None:
    return None if answer is None or answer.score is None else round(answer.score, 3)


def plan(ctx: PlanContext, jev=None, progress: Progress | None = None) -> dict:
    """排出方案；沒有接得上的組合時 plans 是空的，rejected 說明卡在哪裡。"""
    if progress:
        progress("building_candidates", 0, 1)
    candidates, rejected = build_candidates(ctx, progress)
    ranked = judge_candidates(ctx, candidates, jev, progress)
    return {
        "restaurant_source": ctx.restaurant_source,
        "plans": [plan_dict(ctx, c, i + 1) for i, c in enumerate(ranked)],
        "rejected": dict(sorted(rejected.items(), key=lambda kv: -kv[1])),
        "stats": {"showtimes": len(ctx.showtimes), "restaurants": len(ctx.eateries),
                  "combinations": len(ctx.showtimes) * len(ctx.eateries)},
    }

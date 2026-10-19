"""規劃（規格書 7.1）：兩種順序的時間軸、營業時間、空檔排序、候選不重複、Jev 分數與排序、結果格式。"""
import datetime as dt

import pytest

from app.jev.client import Answer
from app.jev.mock import MockJev
from app.planner import (
    ARRIVE_BEFORE_SHOW, CANDIDATES, MEAL_MIN, Eatery, PlanContext, ShowInfo, build_candidates, judge_candidates, plan,
    plan_state, score,
)
from app.routing import Place, Router
from app.timeutil import to_min
from app.transit import TransitGraph

DAY = dt.date(2026, 10, 24)
# 全部在 800 公尺內：都用走路，專心測時間軸
HOME = Place("loc1", "中壢車站", 24.9536, 121.2257)
CINEMA = Place("cin1", "威尼斯影城", 24.9580, 121.2257)  # 北邊約 490 公尺（走 9 分鐘）
CINEMA2 = Place("cin2", "大江影城", 24.9550, 121.2290)


def walk_only() -> Router:
    return Router(TransitGraph([], []), [], use_cache=False)


def show(id_, start, runtime=120, cinema=CINEMA, title=None):
    return ShowInfo(id_, cinema, title or f"片{id_}", "數位", to_min(start), to_min(start) + runtime, 300)


def eatery(id_, open_="11:00", close="22:00", price=400, lat=24.9585):
    open_min = None if open_ is None else to_min(open_)
    close_min = None if close is None else to_min(close)
    return Eatery(id_, Place(f"res{id_}", f"店{id_}", lat, 121.2257), "osm", "japanese", open_min, close_min,
                  f"{open_}–{close}", False, price, "MODERATE")


def ctx(showtimes, eateries, order="movie_first", depart="17:00", party=2, budget=3000):
    return PlanContext(DAY, to_min(depart), HOME, HOME, order, party, budget, showtimes, eateries, walk_only())


def test_movie_first_timeline():
    [c], _ = build_candidates(ctx([show(1, "18:00")], [eatery(1)]))
    assert c.go.arrive == to_min("18:00") - ARRIVE_BEFORE_SHOW  # 開演前 15 分鐘到
    assert c.go.depart == c.go.arrive - c.go.walk_min  # 走路就剛好在那時候出門
    assert c.middle.depart == c.show.end and c.meal_start == c.middle.arrive
    assert c.meal_end == c.meal_start + MEAL_MIN and c.back.depart == c.meal_end
    walking = sum(t.walk_min for t in c.trips)
    assert c.idle == (c.end - to_min("17:00")) - (120 + MEAL_MIN + walking)  # 出發後的空檔


def test_meal_first_leaves_the_restaurant_just_in_time():
    [c], _ = build_candidates(ctx([show(1, "20:00")], [eatery(1)], order="meal_first"))
    assert c.middle.arrive == to_min("20:00") - ARRIVE_BEFORE_SHOW
    assert c.meal_end == c.middle.depart and c.meal_start == c.meal_end - MEAL_MIN
    assert c.go.arrive == c.meal_start and c.back.depart == c.show.end


def test_opening_hours_and_departure_time_are_respected():
    found, rejected = build_candidates(ctx([show(1, "18:00")], [eatery(1, close="20:30"), eatery(2, open_=None)]))
    assert found == []  # 散場 20:00、走過去 20:01，吃 90 分鐘會超過 20:30 打烊；公休的店不排
    assert rejected == {"用餐時段不在營業時間內": 1}
    too_early, rejected = build_candidates(ctx([show(1, "17:10")], [eatery(1)]))
    assert too_early == [] and rejected == {"去程趕不上開演": 1}  # 17:00 出發來不及在 16:55 到


def test_least_idle_first_and_varied():
    shows = [show(1, "18:00"), show(2, "18:00", cinema=CINEMA2), show(3, "19:30"), show(4, "20:00"), show(5, "18:30")]
    eateries = [eatery(i, close="24:00") for i in range(1, 7)]
    found, _ = build_candidates(ctx(shows, eateries))
    assert len(found) == CANDIDATES
    assert [c.idle for c in found] == sorted(c.idle for c in found)  # 空檔少的先
    assert len({c.show.id for c in found}) == CANDIDATES  # 5 個場次都不一樣
    assert len({c.eatery.id for c in found}) == CANDIDATES  # 5 家店也都不一樣
    assert found[0].show.start == to_min("18:00")  # 最早開演的空檔最少


def _answers(time_fit, transit, within, borderline=0.0, decided_by="jev"):
    probs = {"within": within, "borderline": borderline, "over": max(0.0, 1 - within - borderline)}
    return {
        "time_fit": Answer("time_fit", "1", {}, 0.8, time_fit, decided_by, "m"),
        "transit_ease": Answer("transit_ease", "1", {}, 0.8, transit, decided_by, "m"),
        "budget_fit": Answer("budget_fit", "within", probs, 0.8, None, decided_by, "m"),
    }


def test_score_formula():
    assert score(_answers(1.0, 0.5, 0.6, 0.4)) == pytest.approx((1.0 + 0.5 + 0.6 + 0.2) / 3)
    assert score({**_answers(1, 1, 1), "budget_fit": None}) is None


class ScriptedJev:
    """依時間軸裡的片名給分數；「片2」完全沒結果（Jev 和 LLM 都不行）。"""

    def ask(self, state, questions):
        if "片2" in state:
            return {}
        good = "片3" in state
        return _answers(1.0 if good else 0.5, 0.5, 0.9 if good else 0.4)


def test_ranking_by_score_and_unjudged_last():
    shows = [show(1, "18:00"), show(2, "18:30"), show(3, "19:30")]
    candidates, _ = build_candidates(ctx(shows, [eatery(i, close="24:00") for i in (1, 2, 3)]))
    ranked = judge_candidates(ctx(shows, []), candidates, ScriptedJev())
    assert ranked[0].show.id == 3  # 分數最高
    assert ranked[-1].show.id == 2 and ranked[-1].score is None  # 沒結果的排最後
    judged = [c.score for c in ranked if c.score is not None]
    assert judged == sorted(judged, reverse=True)


def test_mock_jev_reads_the_plan_text():
    c = build_candidates(ctx([show(1, "18:00")], [eatery(1, price=400)], party=2, budget=1000))[0][0]
    state = plan_state(ctx([], [], party=2, budget=1000), c)
    assert "最緊的緩衝：15 分鐘" in state and "總花費估計：1400 元" in state and "總預算：1000 元" in state
    answers = MockJev().ask(state, [q for q in __import__("app.jev.questions", fromlist=["x"]).PLAN_QUESTIONS])
    assert answers["time_fit"].choice == "1"  # 提早 15 分鐘是預留的，算剛好
    assert answers["transit_ease"].choice == "2"  # 全程走路
    assert answers["budget_fit"].choice == "over"  # 1,400 元超過 1,000 元


def test_plan_result_format():
    result = plan(ctx([show(1, "18:00")], [eatery(1, price=400)], party=3, budget=3000), MockJev())
    assert result["restaurant_source"] == "osm"
    [p] = result["plans"]
    assert p["rank"] == 1 and p["decided_by"] == "jev" and 0 <= p["score"] <= 1
    assert [i["type"] for i in p["timeline"]] == ["transit", "movie", "transit", "meal", "transit"]
    movie, meal = p["timeline"][1], p["timeline"][3]
    assert movie["starts_at"] == "2026-10-24T18:00:00+08:00" and movie["ticket_price_per_person"] == 300
    assert meal["leave_at"] > meal["seat_at"] and meal["source"] == "osm"
    assert p["timeline"][0]["source"] == "walk_estimate"
    assert p["cost"] == {"tickets_total": 900, "transit_total": 0, "transit_estimated": False,
                         "meal_total_estimate": 1200, "price_level": "MODERATE", "total_estimate": 2100}
    assert set(p["judgment"]) == {"time_fit", "transit_ease", "budget_within_prob", "budget_fit"}
    meal_first = plan(ctx([show(1, "20:00")], [eatery(1)], order="meal_first"), MockJev())["plans"][0]
    assert [i["type"] for i in meal_first["timeline"]] == ["transit", "meal", "transit", "movie", "transit"]


def test_fewer_distinct_options_are_filled_by_least_idle():
    """不重複的場次不夠 5 個時，依空檔補上同一場次、不同餐廳的組合。"""
    found, _ = build_candidates(ctx([show(1, "18:00"), show(2, "18:30")], [eatery(i, close="24:00") for i in range(1, 7)]))
    assert len(found) == CANDIDATES and {c.show.id for c in found} == {1, 2}
    assert len({(c.show.id, c.eatery.id) for c in found}) == CANDIDATES

from app import services
from app.planner import ADS_MIN
from app.schemas import PlanRequest
from app.timeutil import to_min


def _params(saturday, **kwargs):
    return {"date": saturday, "origin": "板橋", "depart_after": "16:00", "home_by": "22:30"} | kwargs


def _plan(session, saturday, **kwargs):
    return services.plan_date(session, PlanRequest(**_params(saturday, **kwargs)))


def _clock(text: str) -> int:
    """「次日 00:10」→ 24:10 的分鐘數。"""
    if text.startswith("次日 "):
        return to_min(text.removeprefix("次日 ")) + 24 * 60
    return to_min(text)


def test_imax_plan_is_consistent(session, saturday):
    res = _plan(session, saturday, movie_query="新片 A", screen_types=["IMAX"])
    assert res["plans"], res["rejections"]
    for p in res["plans"]:
        m = p["movie"]
        assert m["title"] == "新片 A" and m["screen"] == "IMAX"
        assert _clock(p["arrive_home"]) <= to_min("22:30")
        assert _clock(p["leave_home"]) >= to_min("16:00")
        assert _clock(m["end"]) == _clock(m["start"]) + ADS_MIN + m["runtime_min"]
        times = [_clock(s["time"]) for s in p["steps"]]
        assert times == sorted(times), "時間軸要依序往後"


def test_uncertain_judgments_become_questions(session, saturday):
    res = _plan(session, saturday, movie_query="新片 A", screen_types=["IMAX"])
    keys = {c["key"] for c in res["confirmations"]}
    assert any(k.startswith("title_match:") and "序章" in k for k in keys)
    assert "screen_type:label:TITAN 巨幕 英語" in keys


def test_confirming_answer_unlocks_showtimes(session, saturday):
    before = _plan(session, saturday, movie_query="新片 A", screen_types=["IMAX"])
    key = "screen_type:label:TITAN 巨幕 英語"
    after = _plan(session, saturday, movie_query="新片 A", screen_types=["IMAX"], overrides={key: "IMAX"})
    assert key not in {c["key"] for c in after["confirmations"]}
    assert after["filters"]["showtimes"]["kept"] > before["filters"]["showtimes"]["kept"]


def test_impossible_deadline_explains_why(session, saturday):
    res = _plan(session, saturday, home_by="18:00")
    assert res["plans"] == []
    assert res["rejections"]


def test_budget_is_respected(session, saturday):
    res = _plan(session, saturday, budget_per_person=800)
    assert res["plans"]
    assert all(p["cost_per_person"] <= 800 for p in res["plans"])


def test_dinner_first_only(session, saturday):
    res = _plan(session, saturday, meal_order="before", home_by="23:30")
    assert res["plans"]
    assert all(p["order"] == "dinner_first" for p in res["plans"])


def test_cross_city_uses_high_speed_rail(session, saturday):
    res = _plan(session, saturday, origin="台中車站", area="台北市/信義", depart_after="12:00", home_by="24:30")
    assert res["plans"], res["rejections"]
    titles = [s["title"] for s in res["plans"][0]["steps"]]
    assert any("高鐵" in t for t in titles)
    assert not any("車站站" in t for t in titles)


def test_every_area_can_be_planned_from_its_own_city(session, saturday):
    local = {
        "台北市/西門町": "台北車站", "新北市/板橋": "府中", "桃園市/青埔": "中壢",
        "台中市/七期": "台中車站", "台南市/中西區": "永康", "高雄市/三多": "左營",
    }
    for area, origin in local.items():
        res = _plan(session, saturday, origin=origin, area=area, home_by="23:30")
        assert res["plans"], (area, res["rejections"])


def test_station_walk_is_capped(session, saturday):
    from app.planner import STATION_WALK_MAX

    res = _plan(session, saturday, origin="永康", area="台南市/中西區", home_by="23:30")
    for p in res["plans"]:
        walks = [s for s in p["steps"] if s["kind"] == "walk" and "站" in s["title"]]
        assert all(int(s["detail"].split()[1]) <= STATION_WALK_MAX for s in walks)


def test_labels_tell_the_truth(session, saturday):
    cases = [
        {},
        {"origin": "台北車站", "area": "台中市/七期", "depart_after": "12:00", "home_by": "24:30"},
        {"origin": "左營", "area": "高雄市/三多"},
    ]
    for extra in cases:
        plans = _plan(session, saturday, **extra)["plans"]
        assert plans
        cheapest = min(p["cost_per_person"] for p in plans)
        safest = max(p["tightest_buffer_min"] for p in plans)
        for p in plans:
            if "最省" in p["labels"]:
                assert p["cost_per_person"] == cheapest
            if "最穩" in p["labels"]:
                assert p["tightest_buffer_min"] == safest
        assert plans[0]["labels"][0] == "Jev 首選"
        confidences = [p["jev_confidence"] for p in plans]
        assert confidences == sorted(confidences, reverse=True)  # 依 Jev 的組合信心排序


def test_jev_picks_movie_by_genre(session, saturday):
    romance = _plan(session, saturday, genre="愛情")
    assert romance["plans"][0]["movie"]["genre"] == "愛情"
    action = _plan(session, saturday, genre="動作")
    assert action["plans"][0]["movie"]["genre"] == "動作"
    assert action["plans"][0]["jev_choice"]["movie"]["prob"] > 0.5


def test_pinned_movie_skips_movie_choice(session, saturday):
    res = _plan(session, saturday, movie_query="新片 A")
    assert all(p["movie"]["title"] == "新片 A" for p in res["plans"])
    assert all(p["jev_choice"]["movie"]["source"] == "user" for p in res["plans"])
    assert not any(c["key"].startswith("movie_choice:") for c in res["confirmations"])


def test_user_can_switch_jevs_movie(session, saturday):
    first = _plan(session, saturday)
    ask = next(c for c in first["confirmations"] if c["key"].startswith("movie_choice:"))
    other = next(o for o in ask["options"] if o != ask["jev_guess"])
    switched = _plan(session, saturday, overrides={ask["key"]: other})
    assert switched["plans"][0]["movie"]["title"] == other
    assert switched["plans"][0]["jev_choice"]["movie"]["source"] == "user"


def test_diet_filters_restaurants(session, saturday):
    res = _plan(session, saturday, diet="素食")
    assert res["plans"]
    for p in res["plans"]:
        diet = next(a for a in p["jev"] if a["key"].startswith("diet_ok:"))
        assert diet["top"] == "可以"
    assert res["filters"]["restaurants"]["reasons"].get("不符合飲食限制", 0) > 0


def test_note_when_wanted_genre_cannot_fit(session, saturday):
    res = _plan(session, saturday, genre="恐怖")  # 信義的恐怖片 20:40 才開演，22:30 前回不了板橋
    assert res["plans"]
    assert any("恐怖片" in n for n in res["notes"])


def test_overnight_deadline(session, saturday):
    req = PlanRequest(date=saturday, depart_after="19:00", home_by="00:30")
    assert req.home_by == "24:30"


def test_unknown_origin_is_an_error(session, saturday):
    import pytest

    with pytest.raises(ValueError, match="找不到出發站"):
        _plan(session, saturday, origin="火星")


def test_estimate_notes():
    from types import SimpleNamespace as NS

    from app.planner import _estimate_notes

    assert _estimate_notes(NS(estimated=["price"]), NS(estimated=["hours", "price"])) == [
        "票價為估計", "營業時間為估計，出發前請先確認", "價位為估計",
    ]
    assert _estimate_notes(NS(estimated=[]), NS(estimated=[])) == []


def test_plans_carry_estimates(session, saturday):
    res = _plan(session, saturday)
    assert res["plans"]
    for p in res["plans"]:
        assert p["estimates"] == ["車資為估計"]  # 示意資料的票價、餐廳都不是估計，只有車資是
        assert p["movie"]["estimated"] == [] and p["restaurant"]["estimated"] == []


def test_showtimes_outside_the_window_are_skipped_early(session, saturday):
    res = _plan(session, saturday, depart_after="18:00", home_by="22:30")
    reasons = {r["reason"]: r["count"] for r in res["rejections"]}
    assert reasons.get("場次時間落在出門到回家的時間之外", 0) > 0
    for p in res["plans"]:
        assert _clock(p["movie"]["start"]) >= to_min("18:10")


def test_fits_window():
    from types import SimpleNamespace as NS

    from app.planner import _fits_window

    req = PlanRequest(date="2026-10-17", depart_after="18:00", home_by="22:30")
    assert _fits_window(req, NS(start=to_min("18:10"), runtime_min=100))  # 18:10 開演、20:02 散場
    assert not _fits_window(req, NS(start=to_min("18:05"), runtime_min=100))  # 要 17:55 到影城
    assert not _fits_window(req, NS(start=to_min("21:00"), runtime_min=100))  # 22:52 才散場


def _plan_capturing_questions(session, req):
    """跑一次排程，記下問了 Jev 哪些題目（選項多少）。"""
    from app import planner
    from app.jev import JevService

    ctx, _ = services.build_context(session, req, JevService(session))
    asked, original = [], ctx.ask

    def ask(question, subject, context):
        asked.append(question)
        return original(question, subject, context)

    ctx.ask = ask
    return planner.plan(req, ctx), asked


def test_jev_choices_are_capped(session, saturday, monkeypatch):
    from app import planner

    monkeypatch.setattr(planner, "MAX_CHOICE_OPTIONS", 3)
    req = PlanRequest(date=saturday, origin="板橋", depart_after="12:00", home_by="24:30")
    res, asked = _plan_capturing_questions(session, req)
    choices = [q for q in asked if q.key in ("movie_choice", "restaurant_choice")]
    assert {q.key for q in choices} == {"movie_choice", "restaurant_choice"}
    assert all(len(q.options) <= 3 for q in choices)
    assert res["plans"]
    assert res["stats"]["movies_feasible"] > 3  # 統計維持縮減前的數字
    assert any("挑 3 部" in n for n in res["notes"])
    assert any("挑 3 家" in n for n in res["notes"])


def test_top_movies_prefers_wanted_genre_then_buffer(monkeypatch):
    from types import SimpleNamespace as NS

    from app import planner

    monkeypatch.setattr(planner, "MAX_CHOICE_OPTIONS", 2)

    def cand(genre, tightest):
        return NS(showtime=NS(genre=genre, start=1080, cinema=NS(name="影城"), label="數位"), tightest=tightest)

    # 緩衝都在 30 分鐘內，才比得出寬窄（超過 30 分鐘算一樣）
    by_movie = {"甲": [cand("動作", 25)], "乙": [cand("愛情", 10)], "丙": [cand("動作", 20)]}
    assert planner._top_movies(PlanRequest(date="2026-10-13", genre="愛情"), by_movie) == ["乙", "甲"]
    assert planner._top_movies(PlanRequest(date="2026-10-13"), by_movie) == ["甲", "丙"]


def test_top_restaurants_prefer_date_fit_then_walk(monkeypatch):
    """料理類型在前面已經篩過（不符合的店不會進到這裡），所以排序看約會適合度，再看離影城多近。"""
    from types import SimpleNamespace as NS

    from app import planner
    from app.planner import Place

    monkeypatch.setattr(planner, "MAX_CHOICE_OPTIONS", 2)
    cinema = Place(1, "影城", 25.0, 121.5)

    def cand(name, date_fit, lat):
        restaurant = NS(
            place=Place(name, name, lat, 121.5),
            cuisine=NS(top="日式"),
            date_fit=NS(prob=lambda option, p=date_fit: p if option == "適合約會" else 0.0),
        )
        return NS(restaurant=restaurant, showtime=NS(cinema=cinema), tightest=30, walk_total=10, cost=500)

    # 乙、丙約會適合度一樣；乙比較近，但店名排序在丙後面，所以是靠步行距離勝出
    by_name = {"甲": [cand("甲", 0.9, 25.0)], "乙": [cand("乙", 0.5, 25.001)], "丙": [cand("丙", 0.5, 25.006)]}
    assert planner._top_restaurants(PlanRequest(date="2026-10-13"), "某片", by_name) == ["甲", "乙"]


def test_user_choice_survives_trimming(monkeypatch):
    """使用者在改選清單點過的片或餐廳，就算排名掉到上限以外也要留著，否則選擇會被默默丟掉。"""
    from types import SimpleNamespace as NS

    from app import planner
    from app.planner import Place

    monkeypatch.setattr(planner, "MAX_CHOICE_OPTIONS", 2)

    def movie(tightest):
        return NS(showtime=NS(genre="", start=1080, cinema=NS(name="影城"), label="數位"), tightest=tightest)

    by_movie = {"甲": [movie(60)], "乙": [movie(50)], "丙": [movie(5)]}
    req = PlanRequest(date="2026-10-13")
    req.overrides = {f"movie_choice:{planner._subject(req)}": "丙"}
    assert "丙" in planner._top_movies(req, by_movie)

    cinema = Place(1, "影城", 25.0, 121.5)

    def restaurant(name, date_fit):
        r = NS(place=Place(name, name, 25.0, 121.5), cuisine=NS(top="日式"),
               date_fit=NS(prob=lambda option, p=date_fit: p if option == "適合約會" else 0.0))
        return NS(restaurant=r, showtime=NS(cinema=cinema), tightest=30, walk_total=10, cost=500)

    by_name = {"甲": [restaurant("甲", 0.9)], "乙": [restaurant("乙", 0.8)], "丙": [restaurant("丙", 0.1)]}
    req.overrides = {f"restaurant_choice:{planner._subject(req, '某片')}": "丙"}
    assert "丙" in planner._top_restaurants(req, "某片", by_name)


def test_enough_buffer_counts_as_a_tie_so_more_showings_win(monkeypatch):
    """緩衝超過 30 分鐘都算夠寬裕；這時場次多的片排前面，不是下午場特別空的片。"""
    from types import SimpleNamespace as NS

    from app import planner

    monkeypatch.setattr(planner, "MAX_CHOICE_OPTIONS", 1)

    def group(tightest, starts):
        return [NS(showtime=NS(genre="", start=s, cinema=NS(name="影城"), label="數位"), tightest=tightest) for s in starts]

    # 乙場次多、丙下午場特別空；片名排序丙在前，所以乙是靠場次數勝出
    by_movie = {"丙": group(270, [900]), "乙": group(40, [1080, 1200, 1320])}
    assert planner._top_movies(PlanRequest(date="2026-10-13"), by_movie) == ["乙"]


def test_trim_note_does_not_mention_genre_when_none_wanted(session, saturday, monkeypatch):
    from app import planner

    monkeypatch.setattr(planner, "MAX_CHOICE_OPTIONS", 3)
    req = PlanRequest(date=saturday, origin="板橋", depart_after="12:00", home_by="24:30")
    res, asked = _plan_capturing_questions(session, req)
    movie_note = next(n for n in res["notes"] if "挑 3 部" in n)
    assert "片型" not in movie_note
    # 方案裡的片與餐廳都在問過 Jev 的選項裡
    options = {o for q in asked if q.key in ("movie_choice", "restaurant_choice") for o in q.options}
    for p in res["plans"]:
        assert p["movie"]["title"] in options and p["restaurant"]["name"] in options


# ---------- 走法與車資 ----------


def _trip(label, fare):
    from types import SimpleNamespace as NS

    return NS(label=label, fare=fare)


def _search(*options, every=False):
    """假的 TripSearch：every=False 時一開始只有最快的，expand() 之後才有其他走法。"""
    from app.planner import TripSearch

    return TripSearch(lambda e: list(options) if e else list(options[:1]), every)


def test_distinct_drops_slower_and_pricier_trips():
    from app.planner import _distinct

    # 由快到慢：高鐵 → 高鐵轉公車（比較慢、也比較貴）→ 台鐵（慢但便宜）→ 同名的台鐵（更慢）
    options = [_trip("高鐵", 1340), _trip("高鐵 → 公車", 1365), _trip("台鐵", 790), _trip("台鐵", 700)]
    assert [(t.label, t.fare) for t in _distinct(options)] == [("高鐵", 1340), ("台鐵", 790)]


def test_distinct_keeps_at_most_four():
    from app.planner import _distinct

    options = [_trip(f"走法{i}", 1000 - 100 * i) for i in range(6)]
    assert [t.label for t in _distinct(options)] == ["走法0", "走法1", "走法2", "走法3"]


def test_pick_trips_takes_the_fastest_when_affordable():
    from app.planner import RouteChoice, _pick_trips

    go, back = _search(_trip("高鐵", 700), _trip("台鐵", 375)), _search(_trip("高鐵", 700))
    req = PlanRequest(date="2026-10-17", budget_per_person=3000)
    assert [t.label for t in _pick_trips(req, 800, go, back, RouteChoice())] == ["高鐵", "高鐵"]
    assert not go.every  # 付得起就不必找其他走法


def test_budget_limits_the_return_trip():
    from app.planner import RouteChoice, _pick_trips

    go = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    back = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    # 電影加餐費 800：高鐵來回 2200 超出 2000；去程高鐵（800 + 700 + 最便宜回程 375 = 1875）付得起，
    # 回程只剩 500 元，只能搭台鐵
    req = PlanRequest(date="2026-10-17", budget_per_person=2000)
    assert [t.label for t in _pick_trips(req, 800, go, back, RouteChoice())] == ["高鐵", "台鐵"]


def test_budget_switches_both_ways_to_rail():
    from app.planner import RouteChoice, _pick_trips

    go = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    back = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    req = PlanRequest(date="2026-10-17", budget_per_person=1600)
    assert [t.label for t in _pick_trips(req, 800, go, back, RouteChoice())] == ["台鐵", "台鐵"]


def test_trip_over_budget_is_rejected():
    import pytest

    from app.planner import Reject, RouteChoice, _pick_trips

    go = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    back = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    req = PlanRequest(date="2026-10-17", budget_per_person=1500)
    with pytest.raises(Reject, match="^超過每人預算$"):
        _pick_trips(req, 800, go, back, RouteChoice())


def test_pick_trips_rejects_before_expanding_when_the_base_cost_alone_is_over_budget():
    import pytest

    from app.planner import Reject, RouteChoice, _pick_trips

    go = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    back = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    req = PlanRequest(date="2026-10-17", budget_per_person=1500)
    with pytest.raises(Reject, match="^超過每人預算$"):
        _pick_trips(req, 1600, go, back, RouteChoice())  # 電影加餐費 1600 已經超出 1500，不必再找走法
    assert not go.every and not back.every


def test_pick_trips_follows_the_chosen_labels():
    from app.planner import RouteChoice, _pick_trips

    go = _search(_trip("高鐵", 700), _trip("台鐵", 375), every=True)
    back = _search(_trip("高鐵", 700), _trip("台鐵", 375), every=True)
    req = PlanRequest(date="2026-10-17")
    picked = _pick_trips(req, 800, go, back, RouteChoice(every=True, go="台鐵", back="台鐵"))
    assert [t.label for t in picked] == ["台鐵", "台鐵"]
    # 指定的走法付不起時，改用付得起的最快走法
    tight = PlanRequest(date="2026-10-17", budget_per_person=1600)
    picked = _pick_trips(tight, 800, go, back, RouteChoice(every=True, go="高鐵", back="高鐵"))
    assert [t.label for t in picked] == ["台鐵", "台鐵"]


def test_cost_includes_round_trip_fares(session, saturday):
    res = _plan(session, saturday)
    assert res["plans"]
    for p in res["plans"]:
        b = p["cost_breakdown"]
        assert b["movie"] == p["movie"]["price"] and b["meal"] == p["restaurant"]["price"]
        assert b["go"] > 0 and b["back"] > 0  # 板橋到信義要搭捷運
        assert sum(b.values()) == p["cost_per_person"]


def test_transit_steps_show_fares(session, saturday):
    plan = _plan(session, saturday)["plans"][0]
    rides = [s for s in plan["steps"] if s["kind"] == "transit"]
    assert rides and all("車資約" in s["detail"] for s in rides)


# ---------- Jev 選車 ----------

# 台中到信義：去程有高鐵、台鐵兩種走法
TAICHUNG = {"origin": "台中車站", "area": "台北市/信義", "depart_after": "10:00", "home_by": "24:30"}


def test_single_way_to_travel_skips_jev(session, saturday):
    res = _plan(session, saturday)  # 板橋到信義只有捷運板南線
    assert res["plans"]
    for p in res["plans"]:
        for direction in ("go", "back"):
            assert p["jev_choice"][direction]["source"] == "program"
            trip = p["transit"][direction]
            assert [o["label"] for o in trip["options"]] == [trip["choice"]]
    assert not any(c["key"].startswith("transit_") for c in res["confirmations"])


def test_jev_chooses_how_to_travel_when_there_is_a_choice(session, saturday):
    best = _plan(session, saturday, **TAICHUNG)["plans"][0]
    go = best["transit"]["go"]
    assert len(go["options"]) >= 2, go["options"]
    assert go["choice"] in [o["label"] for o in go["options"]]
    assert best["jev_choice"]["go"]["source"] == "mock"
    assert best["jev_choice"]["go"]["choice"] == go["choice"]
    assert best["cost_breakdown"]["go"] == go["fare"]
    assert sum(best["cost_breakdown"].values()) == best["cost_per_person"]


def _fixed_parts(plan):
    return [(s["time"], s["title"]) for s in plan["steps"] if s["kind"] in ("movie", "meal")]


def _picks(res):
    return [(p["movie"]["title"], p["restaurant"]["name"], p["jev_confidence"]) for p in res["plans"]]


def test_switching_trip_keeps_movie_and_meal(session, saturday):
    from app import planner

    before = _plan(session, saturday, **TAICHUNG)
    best = before["plans"][0]
    other = next(o for o in best["transit"]["go"]["options"] if o["label"] != best["transit"]["go"]["choice"])
    req = PlanRequest(**_params(saturday, **TAICHUNG))
    key = f"transit_go:{planner._subject(req, best['movie']['title'], best['restaurant']['name'])}"
    after = _plan(session, saturday, **TAICHUNG, overrides={key: other["label"]})
    switched = after["plans"][0]
    assert switched["transit"]["go"]["choice"] == other["label"]
    assert switched["jev_choice"]["go"]["source"] == "user"
    assert switched["leave_home"] == other["depart"]
    assert switched["cost_breakdown"]["go"] == other["fare"]
    assert _fixed_parts(switched) == _fixed_parts(best)  # 電影、用餐時間不變
    assert _picks(after) == _picks(before)  # 前三名的片、餐廳、組合信心都不變


def test_unsure_trip_choice_is_offered(session, saturday):
    """Jev 選車沒把握時（這裡讓每種走法機率一樣），第一名方案的去程放進「想換換看嗎」。"""
    from app import planner
    from app.jev import JevAnswer, JevService

    req = PlanRequest(**_params(saturday, **TAICHUNG))
    ctx, _ = services.build_context(session, req, JevService(session))
    original = ctx.ask

    def ask(question, subject, context):
        if question.key.startswith("transit_"):
            n = len(question.options)
            return JevAnswer(f"{question.key}:{subject}", question.text, list(question.options), [1 / n] * n)
        return original(question, subject, context)

    ctx.ask = ask
    res = planner.plan(req, ctx)
    item = next(c for c in res["confirmations"] if c["key"].startswith("transit_go:"))
    assert item["kind"] == "choice" and len(item["options"]) >= 2
    assert item["question"] == "Jev 不太確定去程要搭哪一種，你比較想怎麼去？"


def _asked_trips(ctx):
    """包住 ctx.ask，記下每一次選車的（問題, 對象, context）。"""
    asked = []
    original = ctx.ask

    def ask(question, subject, context):
        if question.key.startswith("transit_"):
            asked.append((question, subject, context))
        return original(question, subject, context)

    ctx.ask = ask
    return asked


def _plan_with_trip_texts(session, saturday, **kwargs):
    """排一次台中到信義，回傳（結果, 第一名方案問 Jev 的去程／回程文字）。"""
    from app import planner
    from app.jev import JevService
    from app.jev.service import context_text

    req = PlanRequest(**_params(saturday, **TAICHUNG, **kwargs))
    ctx, _ = services.build_context(session, req, JevService(session))
    asked = _asked_trips(ctx)
    res = planner.plan(req, ctx)
    best = res["plans"][0]
    subject = planner._subject(req, best["movie"]["title"], best["restaurant"]["name"])
    texts = {q.key: context_text(c) for q, s, c in asked if s == subject}
    return res, texts


def test_jev_trip_text_states_outbound_and_budget_left(session, saturday):
    """Jev 只看得到文字：選回程時要說去程搭了什麼、花了多少；有預算時兩個問題都要說剩下多少。"""
    res, texts = _plan_with_trip_texts(session, saturday, budget_per_person=5000)
    best = res["plans"][0]
    assert len(best["transit"]["go"]["options"]) >= 2  # 預算 5000 付得起的去程至少兩種，才會問 Jev
    assert "去程車資每人最多" in texts["transit_go"]
    assert f"去程搭「{best['transit']['go']['choice']}」" in texts["transit_back"]
    assert "回程車資每人最多" in texts["transit_back"]


def test_jev_trip_text_without_budget_still_names_the_outbound(session, saturday):
    res, texts = _plan_with_trip_texts(session, saturday)
    go = res["plans"][0]["transit"]["go"]
    assert "車資每人最多" not in texts["transit_go"]  # 沒設預算就沒有剩下多少的說法
    assert f"去程搭「{go['choice']}」，每人約 {go['fare']} 元" in texts["transit_back"]
    assert "回程車資每人最多" not in texts["transit_back"]


def test_switching_the_outbound_asks_jev_about_the_return_again(session, saturday):
    """改選去程後，回程的文字（連同快取的雜湊）要跟著變，Jev 才會重新回答。"""
    from app import planner

    kwargs = {**TAICHUNG, "budget_per_person": 5000}
    before = _plan(session, saturday, **kwargs)
    best = before["plans"][0]
    other = next(o for o in best["transit"]["go"]["options"] if o["label"] != best["transit"]["go"]["choice"])
    req = PlanRequest(**_params(saturday, **kwargs))
    key = f"transit_go:{planner._subject(req, best['movie']['title'], best['restaurant']['name'])}"

    after = _plan(session, saturday, **kwargs, overrides={key: other["label"]})
    assert after["jev"]["new_calls"] >= 1  # 回程被重新問過，不是沿用去程改選前的快取

    _, texts_before = _plan_with_trip_texts(session, saturday, budget_per_person=5000)
    _, texts_after = _plan_with_trip_texts(session, saturday, budget_per_person=5000, overrides={key: other["label"]})
    assert texts_after["transit_back"] != texts_before["transit_back"]
    assert f"去程搭「{other['label']}」" in texts_after["transit_back"]

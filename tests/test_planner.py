from app import services
from app.planner import ADS_MIN
from app.schemas import PlanRequest
from app.timeutil import to_min


def _plan(session, saturday, **kwargs):
    params = {"date": saturday, "origin": "板橋", "depart_after": "16:00", "home_by": "22:30"} | kwargs
    return services.plan_date(session, PlanRequest(**params))


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


def test_overnight_deadline(session, saturday):
    req = PlanRequest(date=saturday, depart_after="19:00", home_by="00:30")
    assert req.home_by == "24:30"


def test_unknown_origin_is_an_error(session, saturday):
    import pytest

    with pytest.raises(ValueError, match="找不到出發站"):
        _plan(session, saturday, origin="火星")

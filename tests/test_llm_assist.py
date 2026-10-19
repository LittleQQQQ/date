"""Jev 沒把握時請本機 LLM 幫忙判斷（JEV_LLM_ASSIST）。測試不連 LLM：用假的 judge 代替。"""
import time

import httpx
import pytest
from sqlalchemy import delete

from app import llm, services
from app.config import settings
from app.jev import JevAnswer
from app.jev import assist as assist_module
from app.jev.assist import LlmAssist
from app.schemas import PlanRequest

UNSURE = JevAnswer("date_fit:restaurant:1", "這家餐廳適合兩人約會嗎？", ["適合約會", "普通", "不適合"],
                   [0.3, 0.45, 0.25], text="名稱：一樂拉麵\n介紹：日式拉麵店")
SURE = JevAnswer("date_fit:restaurant:2", "這家餐廳適合兩人約會嗎？", ["適合約會", "普通", "不適合"],
                 [0.9, 0.05, 0.05], text="名稱：霧光義式餐館\n介紹：燈光昏黃")


@pytest.fixture
def llm_on(monkeypatch):
    """打開 LLM 協助，並用假的 judge 記下問了什麼；judge.answer 決定回答。測完清掉存好的 LLM 答案。"""
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(settings, "jev_llm_assist", True)
    monkeypatch.setattr(assist_module, "_down_until", 0.0)
    asked = []

    def judge(question, options, text):
        asked.append((question, list(options), text))
        return judge.answer(question, options)

    judge.answer = lambda question, options: options[-1]
    judge.asked = asked
    monkeypatch.setattr(assist_module, "judge", judge)
    yield judge
    from sqlalchemy import inspect

    from app.db import SessionLocal, engine
    from app.models import LlmJudgment

    if inspect(engine).has_table(LlmJudgment.__tablename__):
        with SessionLocal() as s:  # 共用的測試資料庫不要留下假的 LLM 答案
            s.execute(delete(LlmJudgment))
            s.commit()


# ---------- JevAnswer ----------


def test_llm_choice_goes_first_and_keeps_jevs_order():
    ans = UNSURE.with_llm_choice("不適合")
    assert ans.top == "不適合" and ans.source == "llm"
    assert ans.probs == pytest.approx([0.06, 0.09, 0.85])  # LLM 的選擇占八成、Jev 原本的機率占兩成
    assert ans.confident(0.99)  # LLM 決定的就不再請使用者確認
    assert ans.to_dict()["source"] == "llm"


# ---------- LlmAssist ----------


def test_confident_answer_is_left_alone(mem_session, llm_on):
    assert LlmAssist(mem_session).check(SURE) is SURE
    assert llm_on.asked == []


def test_unsure_answer_is_decided_by_llm(mem_session, llm_on):
    helper = LlmAssist(mem_session)
    ans = helper.check(UNSURE)
    assert ans.top == "不適合" and ans.source == "llm"
    assert llm_on.asked == [("這家餐廳適合兩人約會嗎？", ["適合約會", "普通", "不適合"], UNSURE.text)]
    assert helper.calls == 1


def test_llm_answer_is_cached_until_the_text_changes(mem_session, llm_on):
    LlmAssist(mem_session).check(UNSURE)
    mem_session.commit()
    again = LlmAssist(mem_session)
    assert again.check(UNSURE).top == "不適合"
    assert again.calls == 0 and len(llm_on.asked) == 1
    changed = JevAnswer(UNSURE.key, UNSURE.question, UNSURE.options, UNSURE.probs, text="名稱：一樂拉麵\n介紹：改裝成酒吧")
    again.check(changed)
    assert len(llm_on.asked) == 2


def test_call_limit_falls_back_to_jev(mem_session, llm_on):
    helper = LlmAssist(mem_session, max_calls=0)
    assert helper.check(UNSURE) is UNSURE
    assert llm_on.asked == []


def test_llm_failure_falls_back_to_jev(mem_session, llm_on):
    llm_on.answer = lambda question, options: None
    assert LlmAssist(mem_session).check(UNSURE) is UNSURE


@pytest.mark.parametrize("setting, value", [("jev_llm_assist", False), ("ollama_url", "")])
def test_switching_it_off_restores_the_old_behavior(mem_session, llm_on, monkeypatch, setting, value):
    monkeypatch.setattr(settings, setting, value)
    assert LlmAssist(mem_session).check(UNSURE) is UNSURE
    assert llm_on.asked == []


def test_user_answer_is_never_overridden(mem_session, llm_on):
    confirmed = UNSURE.apply_override({UNSURE.key: "普通"})
    assert LlmAssist(mem_session).check(confirmed) is confirmed
    assert llm_on.asked == []


def test_llm_saying_it_cannot_tell_is_remembered(mem_session, llm_on):
    llm_on.answer = lambda question, options: None  # 無法判斷，或答非所問
    assert LlmAssist(mem_session).check(UNSURE) is UNSURE
    mem_session.commit()
    assert LlmAssist(mem_session).check(UNSURE) is UNSURE
    assert len(llm_on.asked) == 1  # 存起來了，不會一直重問


def test_same_question_is_asked_once_per_plan(mem_session, llm_on, monkeypatch):
    def fail(question, options, text):
        llm_on.asked.append(question)
        raise assist_module.LLMUnavailable("timeout")

    monkeypatch.setattr(assist_module, "judge", fail)
    helper = LlmAssist(mem_session)
    helper.check(UNSURE)
    other = JevAnswer("date_fit:restaurant:9", UNSURE.question, UNSURE.options, UNSURE.probs, text="名稱：別家")
    assert helper.check(other) is other
    assert len(llm_on.asked) == 1  # 連不上之後先停一陣子，不是每題都等到逾時


def test_time_budget(mem_session, llm_on):
    helper = LlmAssist(mem_session, deadline=time.monotonic() - 1)
    assert helper.check(UNSURE) is UNSURE
    assert llm_on.asked == []


def test_database_is_not_locked_while_waiting_for_llm(mem_session, llm_on):
    from app.models import JevJudgment

    mem_session.add(JevJudgment(question="q", subject_key="s", input_hash="h", options=[], probs=[], source="mock"))
    pending = []

    def answer(question, options):
        pending.append(bool(mem_session.new or mem_session.dirty))
        return options[0]

    llm_on.answer = answer
    LlmAssist(mem_session).check(UNSURE)
    assert pending == [False]


# ---------- llm.judge ----------


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": self.content}}


class _Client:
    def __init__(self, post):
        self.post = post


def test_judge_returns_one_of_the_options(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    sent = {}

    def post(url, json):
        sent.update(json)
        return _Resp('{"choice": "普通"}')

    monkeypatch.setattr(llm, "_http", lambda: _Client(post))
    assert llm.judge("這家餐廳適合兩人約會嗎？", ["適合約會", "普通", "不適合"], "名稱：一樂拉麵") == "普通"
    assert sent["format"]["properties"]["choice"]["enum"] == ["適合約會", "普通", "不適合", "無法判斷"]
    assert "名稱：一樂拉麵" in sent["messages"][-1]["content"]


def test_judge_without_an_answer(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    for content in ('{"choice": "無法判斷"}', '{"choice": "超適合"}', "not json"):
        monkeypatch.setattr(llm, "_http", lambda content=content: _Client(lambda url, json: _Resp(content)))
        assert llm.judge("q", ["是", "不是"], "t") is None


def test_judge_reports_an_unreachable_llm(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")

    def boom(url, json):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(llm, "_http", lambda: _Client(boom))
    with pytest.raises(llm.LLMUnavailable):
        llm.judge("q", ["是", "不是"], "t")
    monkeypatch.setattr(settings, "ollama_url", "")
    with pytest.raises(llm.LLMUnavailable):
        llm.judge("q", ["是", "不是"], "t")


# ---------- 排程 ----------


def _plan(session, saturday, **kwargs):
    params = {"date": saturday, "origin": "板橋", "depart_after": "16:00", "home_by": "22:30"} | kwargs
    return services.plan_date(session, PlanRequest(**params))


def test_llm_picks_the_movie_when_jev_is_unsure(session, saturday, llm_on):
    """板橋到信義的示意資料裡，Jev 選片沒把握（平常會列出「想換換看嗎」）；LLM 選了最後一個選項。"""
    picked = {}

    def answer(question, options):
        if "哪一部電影" in question:
            picked["movie"] = options[-1]
            return options[-1]
        return options[0]  # 選餐廳、選車也交給 LLM

    llm_on.answer = answer
    res = _plan(session, saturday)
    best = res["plans"][0]
    assert best["movie"]["title"] == picked["movie"]
    assert best["jev_choice"]["movie"]["source"] == "llm"
    assert not any(c["key"].startswith("movie_choice:") for c in res["confirmations"])
    assert res["jev"]["llm_calls"] >= 1


def test_switched_off_plans_like_before(session, saturday, llm_on, monkeypatch):
    monkeypatch.setattr(settings, "jev_llm_assist", False)
    res = _plan(session, saturday)
    assert any(c["key"].startswith("movie_choice:") for c in res["confirmations"])  # 照舊請使用者選
    assert llm_on.asked == [] and res["jev"]["llm_calls"] == 0


def test_llm_answers_request_specific_filters(session, saturday, llm_on):
    """片名比對是排程當下才問的題目，LLM 會幫忙；影廳是固定的題目，只用匯入時先問好的答案。"""
    llm_on.answer = lambda question, options: "不是" if "想看的那部電影" in question else None
    res = _plan(session, saturday, movie_query="新片 A", screen_types=["IMAX"])
    keys = {c["key"] for c in res["confirmations"]}
    assert not any(k.startswith("title_match:") and "序章" in k for k in keys)
    assert "screen_type:label:TITAN 巨幕 英語" in keys  # 匯入時沒有先問過 LLM，所以照舊請使用者確認
    assert not any("TITAN" in text for _, _, text in llm_on.asked)


# ---------- 匯入時先問好 ----------


def test_precompute_asks_llm_for_unsure_genre_and_open_city_restaurants(mem_session, llm_on, monkeypatch):
    from app.collectors.base import AreaRecord, Batch, MovieRecord, RestaurantRecord
    from app.ingest import precompute_jev, upsert_batch
    from app.models import Movie

    monkeypatch.setattr(settings, "active_cities", "桃園市")

    def restaurant(rid, area_id, lat, lng):
        return RestaurantRecord(external_id=rid, name=f"店{rid}", area_id=area_id, lat=lat, lng=lng,
                                description=f"小吃店 {rid}", price_per_person=200, hours={"default": ["11:00", "21:00"]})

    upsert_batch(mem_session, "atmovies", Batch(movies=[
        MovieRecord(external_id="m1", title="週末的尼諾", runtime_min=100, synopsis="一個男孩的週末。"),
    ]))
    upsert_batch(mem_session, "osm", Batch(
        areas=[AreaRecord(id="桃園市/中壢", city="桃園市", name="中壢"), AreaRecord(id="台北市/信義", city="台北市", name="信義")],
        restaurants=[restaurant("ty", "桃園市/中壢", 24.96, 121.22), restaurant("tp", "台北市/信義", 25.03, 121.56)],
    ))
    mem_session.commit()

    llm_on.answer = lambda question, options: options[0]
    precompute_jev(mem_session)

    assert mem_session.query(Movie).one().genre == "動作"  # GENRES 的第一個選項
    texts = [text for _, _, text in llm_on.asked]
    assert any("小吃店 ty" in t for t in texts)
    assert not any("小吃店 tp" in t for t in texts)  # 開放範圍外的縣市不問 LLM

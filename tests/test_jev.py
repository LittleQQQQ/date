"""Jev（規格書 3.1、7.1～7.3）：正式 API 的格式、費用上限、LLM 備援、寫入時的判斷與重算。不連網。"""
import datetime as dt
import json

import httpx
import pytest
from sqlalchemy import select, update

from app.config import settings
from app.ingest import load_network, load_seed
from app.jev import fallback
from app.jev.client import MODEL_VERSION_KEY, Answer, JevUnavailable, TypeSafeJev
from app.jev.classify import judge_pending
from app.jev.mock import MockJev
from app.jev.questions import NONE, OTHER, TIME_FIT, Question, area_question, genre_question
from app.jev.service import judge
from app.models import Area, Cinema, JevJudgment, Movie, Restaurant, point_wkt

pytestmark = pytest.mark.usefixtures("redis_client")

GENRES = [("action", "動作", "打鬥追逐"), ("romance", "愛情", "戀愛")]
AREAS = [("bade", "八德"), ("taoyuan", "桃園"), ("zhongli", "中壢"), ("qingpu", "青埔")]


# ---------- 模擬版 ----------


def test_mock_genre_follows_keywords():
    q = genre_question(GENRES)
    sure = MockJev().ask("片名：特務任務\n劇情簡介：一場追逐與槍戰", [q])["genre"]
    assert sure.choice == "action" and sure.confidence > 0.6
    unsure = MockJev().ask("片名：某部片\n劇情簡介：", [q])["genre"]
    assert unsure.choice == OTHER and unsure.confidence < 0.6  # 沒把握：交給 LLM


def test_mock_area_reads_the_nearest_station():
    q = area_question(AREAS)
    answer = MockJev().ask("影城：威尼斯影城\n地址：桃園市中壢區元化路357號\n最近的車站：太平洋SOGO（區域：中壢，120 公尺）", [q])["area"]
    assert answer.choice == "zhongli" and answer.confidence > 0.9
    far = MockJev().ask("影城：遠方影城\n地址：新竹市東區\n最近的車站：800 公尺內沒有車站", [q])["area"]
    assert far.choice == NONE


# ---------- 正式 API ----------


def _api(handler, waits=None):
    sleep = waits.append if waits is not None else (lambda seconds: None)  # 重試不用真的等
    return TypeSafeJev(api_key="secret-key", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleep)


def test_api_request_and_answers_follow_the_official_format(redis_client):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "jev-1.14.0",
            "answers": {
                "genre": {"type": "choice", "choice": "romance", "confidence": 0.82, "probabilities": {"action": 0.1, "romance": 0.85, "other": 0.05}},
                "time_fit": {"type": "score", "score": 0.75, "confidence": 0.7, "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6}},
            },
            "usage": {"input_tokens": 1000, "output_tokens": 50},
        })

    answers = _api(handler).ask("片名：測試", [genre_question(GENRES), TIME_FIT])
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone" and seen["auth"] == "Bearer secret-key"
    assert seen["body"]["model"] == "jev-latest" and seen["body"]["state"] == "片名：測試"
    assert seen["body"]["questions"]["genre"]["type"] == "choice"
    assert seen["body"]["questions"]["genre"]["criteria"]["other"] == "以上都不是"  # choice：代碼 → 說明
    assert seen["body"]["questions"]["time_fit"]["criteria"] == list(TIME_FIT.criteria)  # score：由低到高的清單
    genre, time_fit = answers["genre"], answers["time_fit"]
    assert (genre.choice, genre.confidence, genre.model) == ("romance", 0.82, "jev-1.14.0")
    assert (time_fit.choice, time_fit.score) == ("2", 0.75)
    assert redis_client.get(MODEL_VERSION_KEY) == "jev-1.14.0"
    assert int(redis_client.get(_budget_key())) == 42  # 1,000 個輸入 token × 每百萬 0.042 美元 = 42 微美元


def _budget_key():
    from app.redis_utils.quota import MonthlyQuota

    return MonthlyQuota("jev_cost_micro_usd", 1).key


def test_api_stops_when_the_monthly_budget_is_used_up(redis_client):
    redis_client.set(_budget_key(), settings.jev_monthly_budget_micro_usd)
    called = []
    with pytest.raises(JevUnavailable, match="費用上限"):
        _api(lambda request: called.append(1)).ask("x", [TIME_FIT])
    assert called == []


def test_api_without_a_key_or_with_errors_is_unavailable():
    with pytest.raises(JevUnavailable, match="TYPESAFE_API_KEY"):
        TypeSafeJev(api_key="").ask("x", [TIME_FIT])
    with pytest.raises(JevUnavailable, match="呼叫失敗"):
        _api(lambda request: httpx.Response(503)).ask("x", [TIME_FIT])


def test_api_retries_timeouts_and_server_errors(redis_client):
    attempts, waits = [], []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("太慢了", request=request)
        if len(attempts) == 2:
            return httpx.Response(502)
        return httpx.Response(200, json={
            "model": "jev-1.14.0", "usage": {"input_tokens": 10},
            "answers": {"time_fit": {"type": "score", "score": 0.5, "confidence": 0.9, "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1}}},
        })

    assert _api(handler, waits).ask("x", [TIME_FIT])["time_fit"].score == 0.5
    assert len(attempts) == 3 and waits == [1.0, 2.0]
    assert redis_client.zcard("rl:jev") == 3  # 每次重試都算進每分鐘上限

    attempts.clear()
    with pytest.raises(JevUnavailable):  # 4xx（例如金鑰錯）不重試
        _api(lambda request: attempts.append(1) or httpx.Response(401), waits).ask("x", [TIME_FIT])
    assert len(attempts) == 1


# ---------- LLM 備援 ----------


class FixedJev:
    def __init__(self, answers=None, error=None):
        self.answers, self.error = answers or {}, error

    def ask(self, state, questions):
        if self.error:
            raise self.error
        return self.answers


def _low(question_id="genre", choice="action", confidence=0.4):
    return Answer(question_id, choice, {choice: confidence}, confidence, None, "jev", "jev-1.14.0")


def test_low_confidence_goes_to_the_llm(monkeypatch):
    llm = Answer("genre", "romance", {"romance": 1.0}, None, None, "llm", "qwen2.5:3b")
    monkeypatch.setattr("app.jev.service.llm_answer", lambda state, q: llm)
    assert judge("x", [genre_question(GENRES)], FixedJev({"genre": _low()}))["genre"] is llm
    sure = _low(confidence=0.9)
    assert judge("x", [genre_question(GENRES)], FixedJev({"genre": sure}))["genre"] is sure  # 有把握就不問 LLM


def test_without_the_llm_keep_jev_or_give_up():
    low = _low()
    assert judge("x", [genre_question(GENRES)], FixedJev({"genre": low}))["genre"] is low  # 測試裡沒開 Ollama
    assert judge("x", [genre_question(GENRES)], FixedJev(error=JevUnavailable("沒金鑰")))["genre"] is None


def test_llm_answers_with_the_same_options(monkeypatch):
    sent = {}

    def handler(request):
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": json.dumps({"choice": "很充裕"}, ensure_ascii=False)}})

    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama.test")
    monkeypatch.setattr(fallback, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(fallback, "_down_until", 0.0)
    answer = fallback.llm_answer("緩衝：60 分鐘", TIME_FIT)
    assert (answer.choice, answer.score, answer.decided_by, answer.confidence) == ("2", 1.0, "llm", None)
    assert sent["body"]["format"]["properties"]["choice"]["enum"][-1] == fallback.ABSTAIN  # 可以回答無法判斷


def test_llm_down_pauses_further_calls(monkeypatch):
    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama.test")
    monkeypatch.setattr(fallback, "_client", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    monkeypatch.setattr(fallback, "_down_until", 0.0)
    with pytest.raises(fallback.LLMUnavailable, match="沒有回應"):
        fallback.llm_answer("x", TIME_FIT)
    with pytest.raises(fallback.LLMUnavailable, match="暫停"):  # 60 秒內不再試
        fallback.llm_answer("x", TIME_FIT)


# ---------- 寫入時的判斷 ----------


def _setup(session):
    load_network(session)
    load_seed(session)
    movie = Movie(atmovies_id="fm1", title="特務任務", runtime_min=110, synopsis="一場追逐與槍戰")
    cinema = Cinema(atmovies_id="t03a01", name="威尼斯影城", address="桃園市中壢區元化路357號", geom=point_wkt(24.9621, 121.2246))
    ramen = Restaurant(source="osm", external_id="node/1", geom=point_wkt(24.9623, 121.2248), osm_name="一樂拉麵",
                       osm_tags={"amenity": "restaurant", "cuisine": "ramen", "name": "一樂拉麵"})
    session.add_all([movie, cinema, ramen])
    session.flush()
    return movie, cinema, ramen


def test_judge_pending_saves_answers_and_writes_them_back(session):
    movie, cinema, ramen = _setup(session)
    stats = judge_pending(session, MockJev())
    assert (stats["movies"], stats["cinemas"], stats["restaurants"]) == (1, 1, 1)
    zhongli = session.scalar(select(Area.id).where(Area.code == "zhongli"))
    assert movie.genre_code == "action"
    assert cinema.area_id == zhongli
    assert (ramen.category_code, ramen.area_id) == ("japanese", zhongli)
    row = session.scalar(select(JevJudgment).where(JevJudgment.subject_type == "restaurant", JevJudgment.question == "cuisine"))
    assert row.decided_by == "jev" and row.model_version == "jev-mock" and row.probabilities["japanese"] > 0.5


def test_judgments_are_redone_only_when_stale(session):
    _setup(session)
    judge_pending(session, MockJev())
    assert judge_pending(session, MockJev()) == {}  # 都判斷過了
    session.execute(update(JevJudgment).where(JevJudgment.subject_type == "movie")
                    .values(judged_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)))
    assert judge_pending(session, MockJev()) == {"by_jev": 1, "movies": 1}  # 超過 30 天就重算


def test_new_jev_version_redoes_jev_answers_but_not_llm_ones(session, monkeypatch):
    _setup(session)
    judge_pending(session, MockJev())
    session.execute(update(JevJudgment).where(JevJudgment.subject_type == "cinema").values(decided_by="llm", model_version="qwen2.5:3b"))
    monkeypatch.setattr("app.jev.classify.model_name", lambda jev: "jev-2.0.0")  # Jev 換版本了
    stats = judge_pending(session, MockJev())
    assert (stats.get("movies"), stats.get("cinemas"), stats.get("restaurants")) == (1, None, 1)


def test_unsure_answers_are_asked_again_the_next_day(session):
    """Jev 沒把握、LLM 也沒幫上忙（測試裡沒開 Ollama）的答案照樣存，但隔天再判斷，不要留一個月。"""
    movie = Movie(atmovies_id="fm9", title="某部片", runtime_min=100, synopsis="")
    load_network(session)
    load_seed(session)
    session.add(movie)
    session.flush()
    judge_pending(session, MockJev())
    assert movie.genre_code == OTHER  # 沒把握的答案也寫回，資料才能用
    assert judge_pending(session, MockJev()) == {}  # 同一天不重問
    session.execute(update(JevJudgment).where(JevJudgment.subject_id == movie.id)
                    .values(judged_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1, minutes=1)))
    assert judge_pending(session, MockJev())["movies"] == 1

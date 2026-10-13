"""聊天輸入的規則解析：桃園的出發站與商圈同名（中壢、八德）或互相包含（桃園／桃園區）時不能判錯。"""
import datetime as dt
import json

import anyio
import pytest

from app import llm, services
from app.llm import rule_parse

TODAY = dt.date(2026, 10, 13)
TAOYUAN = {
    "origins": [
        {"id": "tyn-taoyuan", "name": "桃園", "city": "桃園市", "area": "桃園市/桃園區"},
        {"id": "tyn-zhongli", "name": "中壢", "city": "桃園市", "area": "桃園市/中壢"},
        {"id": "tyn-hsr", "name": "高鐵桃園", "city": "桃園市", "area": "桃園市/青埔"},
        {"id": "tyn-bade", "name": "八德（介壽大智路口）", "city": "桃園市", "area": "桃園市/八德"},
    ],
    "areas": [
        {"id": "桃園市/桃園區", "name": "桃園區", "city": "桃園市"},
        {"id": "桃園市/青埔", "name": "青埔", "city": "桃園市"},
        {"id": "桃園市/中壢", "name": "中壢", "city": "桃園市"},
        {"id": "桃園市/八德", "name": "八德", "city": "桃園市"},
    ],
    "movies": [],
}


@pytest.mark.parametrize(
    ("message", "origin", "area"),
    [
        ("從中壢出發去桃園區", "tyn-zhongli", "桃園市/桃園區"),
        ("從八德出發去中壢看電影", "tyn-bade", "桃園市/中壢"),
        ("從八德出發去桃園區", "tyn-bade", "桃園市/桃園區"),
        ("從桃園出發去中壢", "tyn-taoyuan", "桃園市/中壢"),
        ("從高鐵桃園出發去八德", "tyn-hsr", "桃園市/八德"),
        ("週六從八德（介壽大智路口）出發去青埔", "tyn-bade", "桃園市/青埔"),
    ],
)
def test_taoyuan_origin_and_area_from_the_from_phrase(message, origin, area):
    fields = rule_parse(message, TODAY, TAOYUAN)
    assert (fields["origin"], fields["area"]) == (origin, area)


@pytest.mark.parametrize(
    ("message", "origin", "area"),
    [
        ("八德出發去中壢看電影", "tyn-bade", "桃園市/中壢"),
        ("週六中壢出發，想去桃園區看電影", "tyn-zhongli", "桃園市/桃園區"),
        ("想去桃園區看電影，高鐵桃園出發", "tyn-hsr", "桃園市/桃園區"),
    ],
)
def test_taoyuan_origin_and_area_without_a_from_phrase(message, origin, area):
    fields = rule_parse(message, TODAY, TAOYUAN)
    assert (fields["origin"], fields["area"]) == (origin, area)


def test_station_name_inside_an_area_name_is_not_an_origin():
    """「桃園區」裡的「桃園」是商圈，不是桃園站；句子沒提到出發站就不指定。"""
    fields = rule_parse("週六想去桃園區看電影", TODAY, TAOYUAN)
    assert "origin" not in fields
    assert fields["area"] == "桃園市/桃園區"


def test_station_with_the_same_name_as_an_area_still_counts_as_origin():
    meta = {
        "origins": [{"id": "ntp-banqiao", "name": "板橋", "city": "新北市"}],
        "areas": [
            {"id": "新北市/板橋", "name": "板橋", "city": "新北市"},
            {"id": "台北市/信義", "name": "信義", "city": "台北市"},
        ],
        "movies": [],
    }
    fields = rule_parse("板橋出發去信義看電影", TODAY, meta)
    assert (fields["origin"], fields["area"]) == ("ntp-banqiao", "台北市/信義")


def test_duplicate_station_names_are_told_apart_by_city():
    meta = {
        "origins": [
            {"id": "tpe-cityhall", "name": "市政府", "city": "台北市"},
            {"id": "txg-cityhall", "name": "市政府", "city": "台中市"},
        ],
        "areas": [],
        "movies": [],
    }
    assert rule_parse("從台中市政府出發", TODAY, meta)["origin"] == "txg-cityhall"
    assert rule_parse("我在台北，從市政府出發", TODAY, meta)["origin"] == "tpe-cityhall"
    assert "origin" not in rule_parse("從市政府出發", TODAY, meta)


def test_resolve_station_by_short_name(session):
    assert services.resolve_station(session, "八德").id == "tyn-bade"
    assert services.resolve_station(session, "八德站").id == "tyn-bade"


def _llm_replies(monkeypatch, **fields):
    reply = {
        "date_text": "", "origin": "", "depart_after": "", "home_by": "", "movie_query": "",
        "screen_types": [], "audio": "", "cuisines": [], "budget_per_person": 0, "meal_order": "any",
    } | fields

    async def fake_chat(messages, **kwargs):
        return json.dumps(reply, ensure_ascii=False)

    monkeypatch.setattr(llm, "_ollama_chat", fake_chat)


def test_rules_win_over_llm_for_times_and_budget(monkeypatch):
    """小模型把「十點前到家」寫成 20:00；規則抓得到的時間和預算以規則為準。"""
    _llm_replies(monkeypatch, depart_after="06:00", home_by="20:00", budget_per_person=150)
    message = "從八德出發去中壢，晚上六點以後出門，十點前到家，每人預算1500"
    fields, info = anyio.run(llm.parse_request, message, TODAY, TAOYUAN)
    assert info["mode"] == "llm"
    assert (fields["depart_after"], fields["home_by"], fields["budget_per_person"]) == ("18:00", "22:00", 1500)
    assert {info["sources"][k] for k in ("depart_after", "home_by", "budget_per_person")} == {"規則"}


def test_llm_still_fills_times_the_rules_missed(monkeypatch):
    _llm_replies(monkeypatch, home_by="22:30")
    fields, info = anyio.run(llm.parse_request, "從八德出發去中壢看電影", TODAY, TAOYUAN)
    assert (fields["home_by"], info["sources"]["home_by"]) == ("22:30", "LLM")


@pytest.mark.parametrize(
    ("message", "origin", "area", "defaulted"),
    [
        ("從桃園出發看電影吃韓式", "tyn-taoyuan", "桃園市/桃園區", "area"),  # 出發站服務的商圈
        ("從高鐵桃園出發看電影", "tyn-hsr", "桃園市/青埔", "area"),
        ("想去桃園區看電影", "tyn-taoyuan", "桃園市/桃園區", "origin"),  # 同縣市第一個出發站
    ],
)
def test_missing_place_defaults_to_an_open_one(message, origin, area, defaulted):
    """沒說商圈或出發站時不能用 PlanRequest 的預設值（板橋、台北市/信義），那不在開放範圍內。"""
    fields, info = anyio.run(llm.parse_request, message, TODAY, TAOYUAN)
    assert (fields["origin"], fields["area"]) == (origin, area)
    assert info["sources"][defaulted] == "預設"


def test_origin_area_without_data_falls_back_to_the_same_city():
    meta = {**TAOYUAN, "areas": [a for a in TAOYUAN["areas"] if a["id"] == "桃園市/青埔"]}
    fields, _ = anyio.run(llm.parse_request, "從桃園出發看電影", TODAY, meta)
    assert fields["area"] == "桃園市/青埔"


def test_no_default_area_before_any_area_has_data():
    fields, _ = anyio.run(llm.parse_request, "從桃園出發看電影", TODAY, {**TAOYUAN, "areas": []})
    assert "area" not in fields

"""聊天輸入的規則解析：桃園的出發站與商圈同名（中壢、八德）或互相包含（桃園／桃園區）時不能判錯。"""
import datetime as dt

import pytest

from app import services
from app.llm import rule_parse

TODAY = dt.date(2026, 10, 13)
TAOYUAN = {
    "origins": [
        {"id": "tyn-taoyuan", "name": "桃園", "city": "桃園市"},
        {"id": "tyn-zhongli", "name": "中壢", "city": "桃園市"},
        {"id": "tyn-hsr", "name": "高鐵桃園", "city": "桃園市"},
        {"id": "tyn-bade", "name": "八德（介壽大智路口）", "city": "桃園市"},
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

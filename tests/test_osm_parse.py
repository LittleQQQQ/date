import pytest

from app.collectors.osm import (
    completeness,
    cuisine_category,
    describe,
    estimate_price,
    parse_opening_hours,
    place_hours,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("24/7", {"default": ["00:00", "24:00"]}),
        ("Mo-Su 11:00-21:30", {"default": ["11:00", "21:30"]}),
        ("11:00-21:00", {"default": ["11:00", "21:00"]}),
        (
            "Mo-Fr 11:00-21:00; Sa,Su 10:00-22:00",
            {"default": ["11:00", "21:00"], "sat": ["10:00", "22:00"], "sun": ["10:00", "22:00"]},
        ),
        ("Tu-Su 11:30-21:00; Mo off", {"default": ["11:30", "21:00"], "mon": None}),
        (
            "Su-Th 11:30-22:00; Fr-Sa 11:30-02:00",
            {"default": ["11:30", "22:00"], "fri": ["11:30", "26:00"], "sat": ["11:30", "26:00"]},
        ),
        ("11:30-14:00,17:30-22:00", {"default": ["17:30", "22:00"]}),
        ("11:30-14:00;17:30-22:00", {"default": ["17:30", "22:00"]}),
        ("Mo-Fr 10:00-20:00; PH off", {"default": ["10:00", "20:00"], "sat": None, "sun": None}),
        # PH 只是日期清單裡的一個詞，其他日子的規則照樣生效
        ("Mo-Su 10:00-22:00; PH,Su off", {"default": ["10:00", "22:00"], "sun": None}),
        ("Mo-Su 10:00-22:00; Su,PH off", {"default": ["10:00", "22:00"], "sun": None}),
        ("24/7; PH off", {"default": ["00:00", "24:00"]}),
        ("off", {"default": None}),
        ("Mo-Su 00:00-24:00", {"default": ["00:00", "24:00"]}),
    ],
)
def test_parse_opening_hours(text, expected):
    assert parse_opening_hours(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "sunrise-sunset",
        "Mo-Fr 9h-18h",
        "Jan-Mar 10:00-18:00",
        "Mo-Fr 25:00-26:00",
        "PH off",  # 沒有任何規則適用於星期一到日
        "PH 10:00-14:00",
        "Mo-Fr 24:00-02:00",  # 24:00 以後才開門
        "Mo-Fr 10:00-24:30",  # 超過 24:00 才打烊
        "Mo-Su 10:00-10:00",  # 開門和打烊同一刻
    ],
)
def test_unparseable_hours(text):
    assert parse_opening_hours(text) is None


def test_cuisine_category():
    assert cuisine_category({"amenity": "restaurant", "cuisine": "japanese;ramen"}) == "japanese"
    assert cuisine_category({"amenity": "restaurant", "cuisine": "Hot_Pot"}) == "hotpot_bbq"
    assert cuisine_category({"amenity": "cafe"}) == "cafe_dessert"
    assert cuisine_category({"amenity": "pub", "cuisine": "burger"}) == "bar"
    assert cuisine_category({"amenity": "restaurant", "cuisine": "buffet"}) is None
    assert cuisine_category({"amenity": "restaurant"}) is None


def test_describe():
    tags = {"amenity": "restaurant", "cuisine": "japanese;ramen", "diet:vegetarian": "yes", "reservation": "yes"}
    assert describe(tags, "japanese") == "日式餐廳，供應拉麵，有素食選項，可訂位。"
    assert describe({"amenity": "cafe", "description": "老屋改建，安靜好聊天"}, "cafe_dessert") == "咖啡廳。老屋改建，安靜好聊天。"
    assert describe({"amenity": "fast_food", "takeaway": "only"}, None) == "速食店，以外帶為主。"


def test_estimates():
    assert place_hours({"amenity": "bar"}) == ({"default": ["19:00", "25:00"]}, True)
    assert place_hours({"amenity": "cafe", "opening_hours": "Mo-Su 08:00-18:00"}) == ({"default": ["08:00", "18:00"]}, False)
    assert place_hours({"amenity": "restaurant", "opening_hours": "sunrise-sunset"}) == ({"default": ["11:30", "21:00"]}, True)
    assert place_hours({"amenity": "restaurant", "opening_hours": "PH off"}) == ({"default": ["11:30", "21:00"]}, True)
    assert estimate_price("restaurant", "hotpot_bbq") == estimate_price("restaurant", "western") == 800
    assert estimate_price("restaurant", None) == 500
    assert estimate_price("fast_food", "american") == 200
    assert estimate_price("bar", "bar") == 700


def test_completeness():
    assert completeness({"amenity": "restaurant", "cuisine": "thai", "opening_hours": "24/7"}) == 3
    assert completeness({"amenity": "restaurant", "opening_hours": "24/7"}) == 2
    assert completeness({"amenity": "restaurant", "cuisine": "thai"}) == 1
    assert completeness({"amenity": "restaurant"}) == 0
    assert completeness({"amenity": "restaurant", "opening_hours": "PH off"}) == 0

"""Jev 模擬版：用關鍵字規則產生「選項＋機率」，讓整條流程在拿到正式 Jev API 之前就能跑。

規則刻意寫得很單純，只是為了模擬 Jev「有把握」和「沒把握」兩種情況，不是要取代 Jev。
"""
from __future__ import annotations

import difflib
import math
import re

from .questions import AUDIO, CUISINE, DATE_FIT, MEAL_PACE, SCREEN, TITLE_MATCH, Question


def softmax(logits: list[float]) -> list[float]:
    top = max(logits)
    exps = [math.exp(x - top) for x in logits]
    total = sum(exps)
    return [e / total for e in exps]


def _restaurant_text(context: dict) -> str:
    reviews = context.get("reviews") or []
    return " ".join([context.get("name", ""), context.get("description", ""), *reviews])


def _count(text: str, words: tuple[str, ...]) -> int:
    return sum(text.count(w) for w in words)


DATE_POSITIVE = ("約會", "浪漫", "氣氛", "燈光", "安靜", "夜景", "景觀", "紀念日", "包廂", "調酒", "兩人", "隱私")
DATE_NEGATIVE = ("立食", "排隊", "吵", "擁擠", "外帶", "快餐", "便當", "自助", "翻桌", "聚會", "一群人")


def date_fit(context: dict) -> list[float]:
    text = _restaurant_text(context)
    d = _count(text, DATE_POSITIVE) - _count(text, DATE_NEGATIVE)
    return softmax([0.9 * d, 0.6 - 0.15 * abs(d), -0.9 * d])  # 適合約會 / 普通 / 不適合


PACE_FAST = ("拉麵", "立食", "快餐", "便當", "漢堡", "定食", "出餐快", "出餐很快", "外帶", "自助", "翻桌快", "簡單")
PACE_SLOW = ("燒肉", "火鍋", "麻辣鍋", "套餐", "無菜單", "慢慢", "調酒", "酒館", "限時 100", "聊天", "包廂")


def meal_pace(context: dict) -> list[float]:
    text = _restaurant_text(context)
    fast, slow = _count(text, PACE_FAST), _count(text, PACE_SLOW)
    return softmax([fast - 0.5 * slow, 1.2, slow - 0.5 * fast])  # 快速 / 一般 / 慢食


CUISINE_WORDS = {
    "日式": ("日式", "拉麵", "壽司", "丼", "定食", "居酒屋"),
    "韓式": ("韓式", "炸雞", "部隊鍋", "拌飯", "泡菜"),
    "中式／台式": ("中式", "台式", "點心", "小籠包", "熱炒", "便當", "合菜", "小吃", "牛肉湯", "鍋燒", "麵線"),
    "義式／西式": ("義式", "義大利", "披薩", "燉飯", "西式", "排餐", "牛排", "法式"),
    "美式": ("美式", "漢堡", "薯條"),
    "泰式／南洋": ("泰式", "打拋", "綠咖哩", "越南", "南洋"),
    "火鍋／燒肉": ("火鍋", "麻辣鍋", "個人鍋", "燒肉"),
    "咖啡／甜點": ("咖啡", "甜點", "可麗露", "蛋糕", "下午茶"),
    "酒館": ("酒館", "調酒", "小酌", "下酒菜"),
}


def cuisine(context: dict) -> list[float]:
    text = _restaurant_text(context)
    return softmax([3.0 * _count(text, CUISINE_WORDS[option]) for option in CUISINE.options])


def screen_type(context: dict) -> list[float]:
    label = context["label"].upper()
    imax = 4.0 if "IMAX" in label else 0.0
    fourdx = 4.0 if ("4DX" in label or "MX4D" in label) else 0.0
    normal = 0.0
    if any(w in label for w in ("巨幕", "TITAN", "大廳")):  # 影城自家的大廳，不一定是 IMAX
        imax += 1.2
        normal += 1.0
    elif imax == 0 and fourdx == 0 and any(w in label for w in ("數位", "DIGITAL", "2D", "3D", "一般")):
        normal = 2.5
    return softmax([imax, fourdx, normal])  # IMAX / 4DX / 一般廳


def audio_language(context: dict) -> list[float]:
    label = context["label"]
    dubbed = 3.0 if any(w in label for w in ("國語", "中文版", "配音", "華語")) else 0.0
    original = 3.0 if any(w in label for w in ("英語", "英", "原音", "日語", "韓語", "原文")) else 0.8
    return softmax([original, dubbed])  # 原音 / 國語配音


_TITLE_NOISE = re.compile(r"[\s　《》「」『』：:（）()・\-—,，.。!！?？]")
VARIANT_WORDS = ("序章", "重映", "續集", "前傳", "外傳", "修復", "特別版", "導演版")


def normalize_title(title: str) -> str:
    return _TITLE_NOISE.sub("", title).lower()


def title_match(context: dict) -> list[float]:
    query, title = normalize_title(context["query"]), normalize_title(context["title"])
    if not query or not title:
        return [0.5, 0.5]
    if query == title:
        score = 4.0
    else:
        ratio = difflib.SequenceMatcher(None, query, title).ratio()
        score = 6 * ratio - 3 + (1.0 if (query in title or title in query) else 0.0)
        score -= 0.8 * sum(1 for w in VARIANT_WORDS if w in title and w not in query)
    return softmax([score, 0.0])  # 是 / 不是


SPICY = ("麻辣", "辣", "打拋", "綠咖哩", "酸辣", "部隊鍋")
VEGETARIAN = ("素食", "蔬食", "吃素")
MEAT = ("肉", "雞", "牛", "豬", "魚", "海鮮", "蝦", "串燒", "排餐", "燒肉", "漢堡", "海產")
BEEF = ("牛",)


def diet_ok(context: dict) -> list[float]:
    text = _restaurant_text(context)
    diet = context["diet"]
    if diet == "不吃辣":
        return softmax([1.5 - 2 * _count(text, SPICY), 0.0])  # 可以 / 不行
    if diet == "素食":
        if _count(text, VEGETARIAN):
            return softmax([2.5, 0.0])
        return softmax([-2.0, 0.0]) if _count(text, MEAT) else softmax([0.3, 0.0])  # 沒提到肉也沒提到素：沒把握
    if diet == "不吃牛":
        return softmax([1.5 - 2 * _count(text, BEEF), 0.0])
    return [0.5, 0.5]


# 選擇題的模擬規則：真正的 Jev 會讀 context 的文字自己判斷；模擬版讀程式附上的 _features
GENRE_APPEAL = {"愛情": 0.6, "喜劇": 0.5, "動畫": 0.4, "劇情": 0.3, "動作": 0.2, "科幻": 0.2, "恐怖": -0.3}


def movie_choice(options: tuple[str, ...], context: dict) -> list[float]:
    wanted = context["_wanted_genre"]
    scores = []
    for title in options:
        f = context["_features"][title]
        score = GENRE_APPEAL.get(f["genre"], 0.0)
        if wanted:
            score += 2.5 if f["genre"] == wanted else -0.5
        score += 0.3 if f["premium"] else 0.0
        score += 0.3 * min(f["best_buffer"], 60) / 60
        score -= 0.6 * sum(1 for w in VARIANT_WORDS if w in title)
        scores.append(score)
    return softmax(scores)


def restaurant_choice(options: tuple[str, ...], context: dict) -> list[float]:
    scores = []
    for name in options:
        f = context["_features"][name]
        score = 2.0 * f["date_fit"] + 0.6 * f["date_ok"]
        score += 0.8 if f["cuisine_match"] else 0.0
        if f["budget"] and f["cost"] > 0.9 * f["budget"]:
            score -= 0.6
        score += 0.4 * min(f["buffer"], 60) / 60 - 0.04 * f["walk"]
        scores.append(2.5 * score)  # 放大差距，模擬 Jev 對餐廳有比較明確的偏好
    return softmax(scores)


class MockJev:
    name = "mock"

    _RULES = {
        DATE_FIT.key: date_fit,
        MEAL_PACE.key: meal_pace,
        CUISINE.key: cuisine,
        SCREEN.key: screen_type,
        AUDIO.key: audio_language,
        TITLE_MATCH.key: title_match,
        "diet_ok": diet_ok,
    }
    _CHOICE_RULES = {"movie_choice": movie_choice, "restaurant_choice": restaurant_choice}

    def predict(self, question: Question, context: dict, text: str) -> list[float]:
        if question.key in self._CHOICE_RULES:
            return self._CHOICE_RULES[question.key](question.options, context)
        return self._RULES[question.key](context)

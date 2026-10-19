"""Jev 模擬版：沒有 TypeSafe 金鑰時用，也給測試用（規格書 8.4）。

介面和正式版一樣（ask(state, questions)），用關鍵字規則從 state 文字算出機率，信心值是最高的機率。
只是用來展示流程，判斷品質要等接上正式的 Jev。
"""
from __future__ import annotations

import math
import re

from .client import Answer
from .questions import NONE, OTHER, Question

MODEL = "jev-mock"

GENRE_WORDS = {
    "action": ("動作", "特務", "追逐", "爆破", "槍戰", "格鬥", "復仇", "任務", "劫"),
    "romance": ("愛情", "戀", "愛上", "相愛", "浪漫", "婚"),
    "scifi": ("科幻", "太空", "未來", "外星", "時空", "機器人", "宇宙", "超能力", "英雄"),
    "animation": ("動畫", "劇場版"),
    "comedy": ("喜劇", "爆笑", "搞笑", "笑中帶淚", "荒謬"),
    "drama": ("改編", "真實故事", "人生", "家庭", "成長"),
    "horror": ("恐怖", "驚悚", "鬼", "惡靈", "詛咒", "附身", "怪事"),
}
CUISINE_WORDS = {
    "japanese": ("日式", "日本料理", "拉麵", "壽司", "丼", "定食", "居酒屋", "烏龍麵", "豬排"),
    "korean": ("韓式", "韓國", "炸雞", "部隊鍋", "拌飯", "泡菜"),
    "taiwanese": ("中式", "台式", "點心", "小籠包", "熱炒", "便當", "小吃", "牛肉麵", "麵食", "餃子"),
    "western": ("義式", "義大利", "披薩", "燉飯", "西式", "排餐", "牛排", "法式", "歐式"),
    "american": ("美式", "漢堡", "薯條", "速食"),
    "southeast_asian": ("泰式", "打拋", "綠咖哩", "越南", "南洋", "印度", "印尼", "馬來西亞", "新加坡"),
    "hotpot_bbq": ("火鍋", "麻辣鍋", "個人鍋", "燒肉", "燒烤", "涮涮鍋"),
    "cafe_dessert": ("咖啡", "甜點", "蛋糕", "下午茶", "冰品", "手搖飲"),
    "bar": ("酒館", "酒吧", "調酒", "小酌", "下酒菜"),
}
# 區域：最近的車站寫的區域最準，其次是地址裡的行政區
AREA_WORDS = {"bade": ("八德",), "taoyuan": ("桃園區",), "zhongli": ("中壢",), "qingpu": ("青埔", "大園")}
_STATION_AREA = re.compile(r"最近的車站：[^\n]*?（區域：([^，）]+)")


def softmax(scores: list[float]) -> list[float]:
    top = max(scores)
    exps = [math.exp(s - top) for s in scores]
    total = sum(exps)
    return [e / total for e in exps]


def _count(text: str, words: tuple[str, ...]) -> int:
    return sum(text.count(w) for w in words)


def _by_keywords(question: Question, state: str, words: dict[str, tuple[str, ...]], weight: float, fallback: str) -> list[float]:
    scores = [weight * _count(state, words.get(option, ())) for option in question.options]
    if all(s == 0 for s in scores):  # 一個關鍵字都沒有：偏向「以上都不是」，但沒什麼把握
        scores = [0.8 if option == fallback else 0.0 for option in question.options]
    return softmax(scores)


def _area(question: Question, state: str) -> list[float]:
    if m := _STATION_AREA.search(state):  # 「最近的車站：中壢（區域：中壢，320 公尺）」
        hinted = m.group(1)
        return softmax([4.0 if option != NONE and question.criteria[option].startswith(f"桃園市{hinted}") else 0.0
                        for option in question.options])
    return _by_keywords(question, state, AREA_WORDS, 3.0, NONE)


def _number(state: str, label: str) -> int | None:
    """讀出規劃文字裡的數字，例如「最緊的緩衝：25 分鐘」。"""
    m = re.search(rf"{label}：(\d+)", state)
    return int(m.group(1)) if m else None


def _levels(level: float, count: int) -> list[float]:
    """把 0～(count-1) 之間的等級換成機率：離 level 越近機率越高。"""
    return softmax([-2.5 * abs(i - level) for i in range(count)])


def _time_fit(question: Question, state: str) -> list[float]:
    buffer = _number(state, "最緊的緩衝") or 0
    # 開演前 15 分鐘到是規劃時預留的，算「剛好」；少於這個才算趕，多半小時以上算充裕
    level = 0.0 if buffer < 15 else 1.0 if buffer <= 30 else 2.0
    return _levels(level, len(question.options))


def _transit_ease(question: Question, state: str) -> list[float]:
    transfers, walk, wait = (_number(state, k) or 0 for k in ("轉乘", "步行合計", "等車最久"))
    burden = transfers * 10 + walk + wait  # 每轉乘一次算 10 分鐘的麻煩
    level = 2.0 if burden <= 25 else 1.0 if burden <= 55 else 0.0
    return _levels(level, len(question.options))


def _budget_fit(question: Question, state: str) -> list[float]:
    total, budget = _number(state, "總花費估計"), _number(state, "總預算")
    if not total or not budget:
        return [1 / len(question.options)] * len(question.options)
    ratio = total / budget
    target = "within" if ratio <= 0.85 else "borderline" if ratio <= 1.0 else "over"
    return softmax([3.0 if option == target else 0.0 for option in question.options])


PLAN_RULES = {"time_fit": _time_fit, "transit_ease": _transit_ease, "budget_fit": _budget_fit}


class MockJev:
    def ask(self, state: str, questions: list[Question]) -> dict[str, Answer]:
        answers = {}
        for q in questions:
            if q.id == "genre":
                probs = _by_keywords(q, state, GENRE_WORDS, 1.5, OTHER)
            elif q.id == "cuisine":
                probs = _by_keywords(q, state, CUISINE_WORDS, 3.0, OTHER)
            elif q.id == "area":
                probs = _area(q, state)
            elif q.id in PLAN_RULES:
                probs = PLAN_RULES[q.id](q, state)
            else:  # 不認得的題目：沒有把握，交給 LLM 備援
                probs = [1 / len(q.options)] * len(q.options)
            by_option = dict(zip(q.options, probs))
            choice = max(by_option, key=by_option.get)
            score = None
            if q.type == "score":
                score = sum(i * p for i, p in enumerate(probs)) / (len(probs) - 1)
            answers[q.id] = Answer(q.id, choice, by_option, max(probs), score, "jev", MODEL)
        return answers

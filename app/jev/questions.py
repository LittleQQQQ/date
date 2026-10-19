"""Jev 的題目（規格書 7.1、7.3）。

Jev 不產生文字，只做「選一個」（choice）或「打分數」（score）。本系統只用這兩種，因為它們才有信心值。
- choice：criteria 是「選項代碼 → 說明」
- score：criteria 是由低到高的等級說明，回答是 0～1 的分數
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OTHER = "other"  # 電影種類、餐廳類別都不符合
NONE = "none"  # 不在任何一個區域


@dataclass(frozen=True)
class Question:
    id: str
    type: Literal["choice", "score"]
    instructions: str
    criteria: dict[str, str] | tuple[str, ...]

    @property
    def options(self) -> list[str]:
        """choice 是選項代碼；score 是等級的索引（"0"、"1"、"2"）。"""
        if self.type == "choice":
            return list(self.criteria)
        return [str(i) for i in range(len(self.criteria))]

    def label(self, option: str) -> str:
        """給 LLM 看的選項文字。"""
        if self.type == "choice":
            return f"{option}（{self.criteria[option]}）"
        return self.criteria[int(option)]

    def to_api(self) -> dict:
        criteria = dict(self.criteria) if self.type == "choice" else list(self.criteria)
        return {"type": self.type, "instructions": self.instructions, "criteria": criteria}


# ---------- 寫入時（答案不因人而異，存進 jev_judgments，規格書 7.3） ----------


def genre_question(genres: list[tuple[str, str, str]]) -> Question:
    """genres：categories 表的 (代碼, 名稱, 給 Jev 的說明)。"""
    criteria = {code: f"{label}：{desc}" if desc else label for code, label, desc in genres}
    criteria[OTHER] = "以上都不是"
    return Question("genre", "choice", "這部電影屬於哪一種類型？根據片名和劇情簡介判斷。", criteria)


def cuisine_question(cuisines: list[tuple[str, str, str]]) -> Question:
    criteria = {code: f"{label}：{desc}" if desc else label for code, label, desc in cuisines}
    criteria[OTHER] = "以上都不是"
    return Question("cuisine", "choice", "這家店主要賣哪一類餐點？根據店名和介紹判斷。", criteria)


def area_question(areas: list[tuple[str, str]]) -> Question:
    """areas：(代碼, 名稱)。"""
    criteria = {code: f"桃園市{name}一帶" for code, name in areas}
    criteria[NONE] = "不在上面任何一個區域"
    return Question("area", "choice", "這個地點屬於哪一個區域？根據地址和最近的車站判斷。", criteria)


# ---------- 規劃時（每個候選問一次，不存，規格書 7.1） ----------

TIME_FIT = Question("time_fit", "score", "這個約會行程的時間安排如何？", ("很趕，可能來不及", "剛好", "很充裕"))
TRANSIT_EASE = Question("transit_ease", "score", "這個約會行程的交通方便嗎？", ("轉乘麻煩或走很久", "普通", "很方便"))
BUDGET_FIT = Question("budget_fit", "choice", "這個約會行程的總花費和預算比起來如何？", {
    "within": "在預算內",
    "borderline": "接近預算上限",
    "over": "超出預算",
})
PLAN_QUESTIONS = (TIME_FIT, TRANSIT_EASE, BUDGET_FIT)

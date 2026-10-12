"""Jev 要回答的封閉式問題，以及回答的資料格式。

Jev 只做一件事：讀一段文字，對固定選項給出機率。它不抓網頁、不產生文字或數字。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    options: tuple[str, ...]


DATE_FIT = Question("date_fit", "這家餐廳適合兩人約會嗎？", ("適合約會", "普通", "不適合"))
MEAL_PACE = Question("meal_pace", "在這家餐廳吃一餐大約要多久？", ("快速", "一般", "慢食"))
CUISINE = Question(
    "cuisine",
    "這家餐廳屬於哪一類料理？",
    ("日式", "韓式", "中式／台式", "義式／西式", "美式", "泰式／南洋", "火鍋／燒肉", "咖啡／甜點", "酒館"),
)
SCREEN = Question("screen_type", "這個場次是哪一種影廳？", ("IMAX", "4DX", "一般廳"))
AUDIO = Question("audio_language", "這個場次是原音還是配音？", ("原音", "國語配音"))
TITLE_MATCH = Question("title_match", "這個場次的片名，是不是使用者想看的那部電影？", ("是", "不是"))

# 用餐快慢 → 預估用餐分鐘數（程式計算用，不是 Jev 產生的數字）
MEAL_MINUTES = {"快速": 40, "一般": 60, "慢食": 90}

# 使用者可以選的偏好（給 Jev 做選擇時參考）
GENRES = ("動作", "愛情", "科幻", "動畫", "喜劇", "劇情", "恐怖")
DIETS = ("不吃辣", "素食", "不吃牛")

# 開眼電影網沒有片型，匯入時由 Jev 讀片名和簡介判斷
GENRE = Question("genre", "這部電影屬於哪一種片型？", GENRES)


def diet_question(diet: str) -> Question:
    return Question("diet_ok", f"這家餐廳能滿足「{diet}」的需求嗎？", ("可以", "不行"))


# ---------- 選擇題：選項每次不同，由程式先篩出排得進去的候選 ----------


def movie_choice_question(titles: list[str]) -> Question:
    return Question("movie_choice", "根據使用者條件，這次約會最適合看下面哪一部電影？", tuple(titles))


def restaurant_choice_question(movie: str, names: list[str]) -> Question:
    return Question("restaurant_choice", f"這次約會看《{movie}》，最適合去下面哪一家吃飯？", tuple(names))


@dataclass
class JevAnswer:
    key: str  # 題目＋對象，例如 date_fit:restaurant:3；使用者確認答案時也用這個 key
    question: str
    options: list[str]
    probs: list[float]
    source: str = "mock"  # mock / api / user
    overridden: bool = False

    @property
    def top(self) -> str:
        return self.options[max(range(len(self.probs)), key=self.probs.__getitem__)]

    @property
    def confidence(self) -> float:
        return max(self.probs)

    def prob(self, option: str) -> float:
        return self.probs[self.options.index(option)] if option in self.options else 0.0

    def confident(self, threshold: float) -> bool:
        return self.overridden or self.confidence >= threshold

    def apply_override(self, overrides: dict[str, str]) -> JevAnswer:
        """使用者已經確認過答案，就用使用者的答案取代 Jev 的猜測。"""
        choice = overrides.get(self.key)
        if choice not in self.options:
            return self
        probs = [1.0 if option == choice else 0.0 for option in self.options]
        return JevAnswer(self.key, self.question, self.options, probs, source="user", overridden=True)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "question": self.question,
            "options": self.options,
            "probs": [round(p, 3) for p in self.probs],
            "top": self.top,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "overridden": self.overridden,
        }

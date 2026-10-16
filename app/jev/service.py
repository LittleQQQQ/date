"""Jev 的統一入口：選擇模擬版或 API 版，並把結果快取在 jev_judgments 表。

同一個對象（例如某家餐廳）的原始文字沒變，就直接用快取；資料被爬蟲更新後，文字變了才會重新問 Jev。
"""
from __future__ import annotations

import hashlib
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import JevJudgment, Movie, Restaurant
from ..timeutil import now_tw
from .api_client import JevApiClient
from .mock import MockJev, normalize_title
from .questions import JevAnswer, Question


class JevBackend(Protocol):
    name: str

    def predict(self, question: Question, context: dict, text: str) -> list[float]: ...


def make_backend() -> JevBackend:
    if settings.jev_mode == "api":
        return JevApiClient(settings.jev_api_url, settings.jev_api_key, settings.jev_timeout)
    return MockJev()


# ---------- 判斷對象：subject key 與要給 Jev 讀的文字 ----------

_CONTEXT_LABELS = {
    "name": "名稱",
    "description": "介紹",
    "reviews": "評論",
    "synopsis": "簡介",
    "label": "場次標示",
    "query": "使用者想看",
    "title": "場次片名",
    "diet": "飲食限制",
    "user": "使用者條件",
    "movie": "要看的電影",
    "plan": "這個方案",
    "options": "各選項說明",
}


def context_text(context: dict) -> str:
    """把 context 轉成給 Jev 讀的文字。底線開頭的 key 是給模擬版用的數值，不放進文字。"""
    lines = []
    for key, value in context.items():
        if key.startswith("_"):
            continue
        if key == "options":
            value = "\n" + "\n".join(f"- {v}" for v in value)
        elif isinstance(value, list):
            value = "；".join(str(v) for v in value)
        lines.append(f"{_CONTEXT_LABELS.get(key, key)}：{value}")
    return "\n".join(lines)


def restaurant_subject(r: Restaurant) -> str:
    return f"restaurant:{r.id}"


def place_subject(place_id: str) -> str:
    """Google 即時查到的餐廳沒有存進資料庫，改用 place_id（Google 條款允許長期保存的只有它）。"""
    return f"restaurant:google:{place_id}"


def restaurant_context(r: Restaurant) -> dict:
    return {"name": r.name, "description": r.description, "reviews": list(r.reviews or [])}


def movie_subject(m: Movie) -> str:
    return f"movie:{m.id}"


def movie_context(m: Movie) -> dict:
    return {"movie": m.title, "synopsis": m.synopsis}


def label_subject(label: str) -> str:
    return f"label:{label}"


def title_subject(raw_title: str, query: str) -> str:
    return f"title:{normalize_title(raw_title)}|q:{normalize_title(query)}"


def _normalize(probs: list[float]) -> list[float]:
    clipped = [max(0.0, float(p)) for p in probs]
    total = sum(clipped) or 1.0
    return [round(p / total, 4) for p in clipped]


class JevService:
    def __init__(self, session: Session, backend: JevBackend | None = None):
        self.session = session
        self.backend = backend or make_backend()
        self.calls = 0  # 這次實際呼叫 Jev 的次數（沒用到快取的）

    def ask(self, question: Question, subject: str, context: dict) -> JevAnswer:
        text = context_text(context)
        digest = hashlib.sha256(
            "\n".join([question.key, question.text, "|".join(question.options), text]).encode("utf-8")
        ).hexdigest()
        key = f"{question.key}:{subject}"

        row = self.session.scalar(
            select(JevJudgment).where(JevJudgment.question == question.key, JevJudgment.subject_key == subject)
        )
        if row and row.input_hash == digest and row.source == self.backend.name:
            return JevAnswer(key, question.text, list(row.options), list(row.probs), row.source)

        probs = _normalize(self.backend.predict(question, context, text))
        self.calls += 1
        if row is None:
            row = JevJudgment(question=question.key, subject_key=subject)
            self.session.add(row)
        row.input_hash = digest
        row.options = list(question.options)
        row.probs = probs
        row.source = self.backend.name
        row.updated_at = now_tw()
        return JevAnswer(key, question.text, list(question.options), probs, self.backend.name)

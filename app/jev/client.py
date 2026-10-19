"""TypeSafe Jev 正式 API（規格書 3.1）。格式照官方文件 https://docs.typesafe.ai/introduction/quickstart：

POST https://api.typesafe.ai/v1/systemone，Authorization: Bearer {TYPESAFE_API_KEY}
{"model": "jev-latest", "state": "…", "questions": {"題目id": {"type": "choice", "instructions": "…", "criteria": {…}}}}
→ {"model": "jev-1.14.0", "answers": {"題目id": {"type": "choice", "choice": "…", "confidence": 0.9, "probabilities": {…}}},
   "usage": {"input_tokens": 428, "output_tokens": 73}}
score 題回 {"score": 0.67, "confidence": …, "probabilities": {"0": …, "1": …}}。

- 一次可以問好幾題，題目平行處理。
- 碰到每分鐘上限（rl:jev）就排隊等；每月費用（quota:jev_cost_micro_usd）用完就不再呼叫，改由 LLM 備援。
- 計費只算輸入 token，每百萬 token 約 0.042 美元。
- 回應的模型版本記在 Redis（jev:model_version）：版本改變時，寫入時的判斷要全部重算。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import httpx
import redis

from ..config import settings
from ..redis_utils import get_redis
from ..redis_utils.quota import MonthlyQuota
from ..redis_utils.ratelimit import SlidingWindow
from .questions import Question

API_URL = "https://api.typesafe.ai/v1/systemone"
MICRO_USD_PER_INPUT_TOKEN = 0.042  # 每百萬 token 0.042 美元
MODEL_VERSION_KEY = "jev:model_version"


class JevUnavailable(Exception):
    """Jev 這次不能用（沒有金鑰、每月費用用完、連線失敗）。呼叫端改用 LLM 備援。"""


@dataclass
class Answer:
    question: str
    choice: str  # choice 題是選項代碼；score 題是機率最高的等級索引（"0"、"1"…）
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None  # LLM 判斷的是 None
    score: float | None = None  # 只有 score 題有：0～1
    decided_by: str = "jev"  # jev 或 llm
    model: str = ""


class TypeSafeJev:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None,
                 redis_client: redis.Redis | None = None):
        self.api_key = settings.typesafe_api_key if api_key is None else api_key
        self.http = client or httpx.Client(timeout=settings.jev_timeout)
        self.redis = redis_client or get_redis()
        self.limiter = SlidingWindow("rl:jev", settings.jev_rate_limit_per_minute, 60, self.redis)
        self.budget = MonthlyQuota("jev_cost_micro_usd", settings.jev_monthly_budget_micro_usd, self.redis)

    def ask(self, state: str, questions: list[Question]) -> dict[str, Answer]:
        if not self.api_key.strip():
            raise JevUnavailable("沒有設定 TYPESAFE_API_KEY")
        if not self.budget.available():
            raise JevUnavailable("這個月的 Jev 費用上限用完了")
        self.limiter.wait()
        body = {"model": settings.jev_model, "state": state, "questions": {q.id: q.to_api() for q in questions}}
        try:
            resp = self.http.post(API_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JevUnavailable(f"Jev 呼叫失敗：{type(exc).__name__}") from exc
        tokens = int(data.get("usage", {}).get("input_tokens", 0))
        self.budget.consume(math.ceil(tokens * MICRO_USD_PER_INPUT_TOKEN))
        model = data.get("model", settings.jev_model)
        self.redis.set(MODEL_VERSION_KEY, model)
        return {q.id: _parse(q, data["answers"][q.id], model) for q in questions if q.id in data.get("answers", {})}


def _parse(question: Question, raw: dict, model: str) -> Answer:
    probs = {str(k): float(v) for k, v in raw.get("probabilities", {}).items()}
    if question.type == "choice":
        choice = raw["choice"]
        score = None
    else:
        score = float(raw["score"])
        choice = max(probs, key=probs.get) if probs else str(round(score * (len(question.criteria) - 1)))
    return Answer(question.id, choice, probs, raw.get("confidence"), score, "jev", model)


def current_model_version(client: redis.Redis | None = None) -> str | None:
    """最近一次 Jev 回應的模型版本；還沒呼叫過是 None。"""
    return (client or get_redis()).get(MODEL_VERSION_KEY)

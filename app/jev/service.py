"""問 Jev，沒把握再問 LLM（規格書 3.1、7.1）。

- 信心值低於 JEV_CONFIDENCE_THRESHOLD（預設 0.6），或 Jev 這次不能用（沒金鑰、費用用完、連線失敗），就改問 LLM。
- LLM 也沒結果（沒開、逾時、說無法判斷）：有 Jev 的答案就照用 Jev 的，沒有就是 None，由呼叫端決定怎麼辦
  （寫入時下次再判斷；規劃時把候選排到最後）。
"""
from __future__ import annotations

import logging

from ..config import settings
from .client import Answer, JevUnavailable, TypeSafeJev
from .fallback import LLMUnavailable, llm_answer
from .mock import MODEL as MOCK_MODEL
from .mock import MockJev
from .questions import Question

log = logging.getLogger(__name__)


def get_jev() -> MockJev | TypeSafeJev:
    return TypeSafeJev() if settings.jev_mode == "api" else MockJev()


def model_name(jev: MockJev | TypeSafeJev) -> str | None:
    """現在用的 Jev 版本；正式版要等呼叫過一次才知道（回應裡的 model），還不知道是 None。"""
    if isinstance(jev, MockJev):
        return MOCK_MODEL
    from .client import current_model_version

    return current_model_version(jev.redis)


def judge(state: str, questions: list[Question], jev: MockJev | TypeSafeJev | None = None) -> dict[str, Answer | None]:
    jev = jev or get_jev()
    try:
        answers = jev.ask(state, questions)
    except JevUnavailable as exc:
        log.warning("Jev 不能用，改問 LLM：%s", exc)
        answers = {}
    result: dict[str, Answer | None] = {}
    for q in questions:
        answer = answers.get(q.id)
        if answer is None or (answer.confidence is not None and answer.confidence < settings.jev_confidence_threshold):
            try:
                answer = llm_answer(state, q) or answer
            except LLMUnavailable:
                pass
        result[q.id] = answer
    return result

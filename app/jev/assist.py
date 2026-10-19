"""Jev 沒把握時，請本機 LLM 幫忙判斷（設定 JEV_LLM_ASSIST，預設開啟）。

- LLM 讀 Jev 讀過的同一段文字、回答同一題、從同樣的選項選一個（app/llm.py 的 judge）。
- LLM 選的答案排第一（JevAnswer.with_llm_choice），之後就不再請使用者確認。
- 結果存在 llm_judgments 表：同一題、同一段文字、同一個模型不會再問一次。
- LLM 沒開、逾時、答非所問，或這次可以問的題數用完了，就保留 Jev 原本沒把握的答案，照舊請使用者確認。
- 使用者確認過的答案一律不動。
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..llm import judge
from ..models import LlmJudgment
from ..timeutil import now_tw
from .questions import JevAnswer


def _digest(ans: JevAnswer) -> str:
    return hashlib.sha256("\n".join([ans.question, "|".join(ans.options), ans.text]).encode("utf-8")).hexdigest()


class LlmAssist:
    def __init__(self, session: Session, max_calls: int | None = None):
        """max_calls：這個物件最多實際呼叫 LLM 幾次（存過的答案不算）；None 是不限，0 是只用存過的答案。"""
        self.session = session
        self.max_calls = max_calls
        self.calls = 0  # 實際呼叫 LLM 的次數

    def check(self, ans: JevAnswer) -> JevAnswer:
        """Jev 有把握（或使用者確認過）就原樣回傳；沒把握就請 LLM 幫忙選。"""
        threshold = settings.jev_confidence_threshold
        if not (settings.jev_llm_assist and settings.ollama_url) or ans.confident(threshold) or not ans.text:
            return ans
        if len(ans.options) < 2:
            return ans
        digest = _digest(ans)
        row = self.session.scalar(select(LlmJudgment).where(LlmJudgment.key == ans.key))
        if row and row.input_hash == digest and row.model == settings.ollama_model and row.choice in ans.options:
            return ans.with_llm_choice(row.choice)
        if self.max_calls is not None and self.calls >= self.max_calls:
            return ans
        self.calls += 1
        choice = judge(ans.question, ans.options, ans.text)
        if choice is None:
            return ans
        if row is None:
            row = LlmJudgment(key=ans.key)
            self.session.add(row)
        row.input_hash, row.model, row.choice, row.updated_at = digest, settings.ollama_model, choice, now_tw()
        return ans.with_llm_choice(choice)

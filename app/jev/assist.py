"""Jev 沒把握時，請本機 LLM 幫忙判斷（設定 JEV_LLM_ASSIST，預設開啟）。

- LLM 讀 Jev 讀過的同一段文字、回答同一題、從同樣的選項選一個（app/llm.py 的 judge），資料不夠可以回答「無法判斷」。
- LLM 選的答案排第一（JevAnswer.with_llm_choice），之後就不再請使用者確認。
- LLM 說無法判斷、答非所問、沒開、逾時，或這次能問的題數和時間用完了，就保留 Jev 原本沒把握的答案，照舊請使用者確認。
- 使用者確認過的答案一律不動。
- 答案（包括「無法判斷」）存在 llm_judgments 表：同一題、同一段文字、同一個模型不會再問一次。
- Ollama 連不上或逾時後，COOLDOWN 秒內不再嘗試，免得每一題都等到逾時。
- 呼叫 LLM 前先提交資料庫：等 LLM 的這幾秒不佔著 SQLite 的寫入鎖，背景更新和其他請求才寫得進去。
"""
from __future__ import annotations

import hashlib
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..llm import JUDGE_PROMPT_VERSION, LLMUnavailable, judge
from ..models import LlmJudgment
from ..timeutil import now_tw
from .questions import JevAnswer

COOLDOWN = 60.0  # 秒
PLAN_BUDGET = 30.0  # 秒；一次排程最多花多少時間等 LLM（見 services.build_context）
_down_until = 0.0  # time.monotonic()；在這之前不呼叫 LLM


def _digest(ans: JevAnswer) -> str:
    parts = [str(JUDGE_PROMPT_VERSION), ans.question, "|".join(ans.options), ans.text]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _commit(session: Session) -> None:
    """提交目前的寫入；同一題剛好被另一個請求先存了，就放掉自己的（都只是快取）。"""
    try:
        session.commit()
    except IntegrityError:
        session.rollback()


class LlmAssist:
    def __init__(self, session: Session, max_calls: int | None = None, deadline: float | None = None):
        """max_calls：最多實際呼叫 LLM 幾次（存好的答案不算），None 是不限、0 是只讀存好的答案。
        deadline：time.monotonic() 的截止時間，過了就不再呼叫；None 是不限。"""
        self.session = session
        self.max_calls = max_calls
        self.deadline = deadline
        self.calls = 0  # 實際呼叫 LLM 的次數
        self._tried: set[str] = set()  # 這個物件已經問過的題目，同一題不再問第二次

    def check(self, ans: JevAnswer) -> JevAnswer:
        """Jev 有把握（或使用者確認過）就原樣回傳；沒把握就請 LLM 幫忙選。"""
        global _down_until
        threshold = settings.jev_confidence_threshold
        if not (settings.jev_llm_assist and settings.ollama_url) or ans.confident(threshold) or not ans.text:
            return ans
        if len(ans.options) < 2:
            return ans
        digest = _digest(ans)
        row = self.session.scalar(select(LlmJudgment).where(LlmJudgment.key == ans.key))
        if row and row.input_hash == digest and row.model == settings.ollama_model:
            return ans.with_llm_choice(row.choice) if row.choice in ans.options else ans  # 空字串＝LLM 說無法判斷
        if not self._may_call(ans.key):
            return ans
        self.calls += 1
        self._tried.add(ans.key)
        _commit(self.session)  # 等 LLM 時不佔著寫入鎖
        try:
            choice = judge(ans.question, ans.options, ans.text)
        except LLMUnavailable:
            _down_until = time.monotonic() + COOLDOWN
            return ans
        row = self.session.scalar(select(LlmJudgment).where(LlmJudgment.key == ans.key)) or LlmJudgment(key=ans.key)
        row.input_hash, row.model, row.choice, row.updated_at = digest, settings.ollama_model, choice or "", now_tw()
        self.session.add(row)
        _commit(self.session)
        return ans.with_llm_choice(choice) if choice else ans

    def _may_call(self, key: str) -> bool:
        now = time.monotonic()
        if key in self._tried or now < _down_until:
            return False
        if self.max_calls is not None and self.calls >= self.max_calls:
            return False
        return self.deadline is None or now < self.deadline

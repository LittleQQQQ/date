"""LLM 備援（規格書 3.1、7.1）：Jev 信心值低於門檻，或 Jev 這次不能用時，改請本機 Ollama 回答同一題。

沿用舊版 feat/jev-llm-assist 的做法：
- LLM 讀和 Jev 同一段文字、從同樣的選項選一個；資料不夠可以回答「無法判斷」，不要硬選。
- Ollama 連不上或逾時後，COOLDOWN 秒內不再嘗試，免得每一題都等到逾時。
- 回傳的答案標記 decided_by = llm、信心值是 None（LLM 沒有可靠的信心值）。
"""
from __future__ import annotations

import json
import time

import httpx

from ..config import settings
from .client import Answer
from .questions import Question

ABSTAIN = "無法判斷"
COOLDOWN = 60.0  # 秒
_SYSTEM = (
    "你幫忙回答封閉式的判斷題。只根據使用者給的資料判斷，不要猜資料裡沒有的事；"
    f"資料不足以判斷時，回答「{ABSTAIN}」。用 JSON 回答 {{\"choice\": 選項}}。"
)
_down_until = 0.0  # time.monotonic()；在這之前不呼叫 LLM
_client: httpx.Client | None = None


class LLMUnavailable(Exception):
    pass


def _http() -> httpx.Client:
    """共用一個連線（keep-alive）：每題重新連線在 Windows 上要多花兩秒。"""
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(settings.ollama_timeout, connect=1.0))
    return _client


def available() -> bool:
    return bool(settings.ollama_base_url) and time.monotonic() >= _down_until


def llm_answer(state: str, question: Question) -> Answer | None:
    """請 LLM 回答一題。LLM 說無法判斷或答非所問回 None；Ollama 沒設定、連不上、逾時丟 LLMUnavailable。"""
    global _down_until
    if not settings.ollama_base_url:
        raise LLMUnavailable("沒有設定 OLLAMA_BASE_URL")
    if time.monotonic() < _down_until:
        raise LLMUnavailable("Ollama 剛剛連不上，暫停中")
    labels = {question.label(o): o for o in question.options}
    choices = [*labels, ABSTAIN]
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": 4096},
        "keep_alive": "10m",
        "format": {"type": "object", "properties": {"choice": {"type": "string", "enum": choices}}, "required": ["choice"]},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"問題：{question.instructions}\n選項：{'、'.join(choices)}\n資料：\n{state}"},
        ],
    }
    try:
        resp = _http().post(f"{settings.ollama_base_url.rstrip('/')}/api/chat", json=payload)
        resp.raise_for_status()
    except Exception as exc:  # 連不上、逾時、網址寫錯……都當作 LLM 現在不能用
        _down_until = time.monotonic() + COOLDOWN
        raise LLMUnavailable(f"Ollama 沒有回應：{type(exc).__name__}") from exc
    try:
        picked = json.loads(resp.json()["message"]["content"])["choice"]
    except (ValueError, KeyError, TypeError):
        return None
    option = labels.get(picked)
    if option is None:
        return None
    score = int(option) / (len(question.options) - 1) if question.type == "score" else None
    return Answer(question.id, option, {option: 1.0}, None, score, "llm", settings.ollama_model)

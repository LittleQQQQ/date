"""正式 Jev API 的用戶端。

注意：下面的請求與回應格式是「假設」的。拿到正式規格後，只要改這個檔案，其他程式都不用動。

    POST {JEV_API_URL}/predict
    Authorization: Bearer {JEV_API_KEY}        （有設定才送）
    {"question": "這家餐廳適合兩人約會嗎？",
     "options": ["適合約會", "普通", "不適合"],
     "context": "名稱：……\n介紹：……\n評論：……"}

    回應：{"probabilities": [0.82, 0.15, 0.03]}
      或：{"probabilities": {"適合約會": 0.82, "普通": 0.15, "不適合": 0.03}}
"""
from __future__ import annotations

import httpx

from .questions import Question


class JevApiClient:
    name = "api"

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 10.0):
        if not base_url:
            raise ValueError("JEV_MODE=api 時必須在 .env 設定 JEV_API_URL")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def predict(self, question: Question, context: dict, text: str) -> list[float]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {"question": question.text, "options": list(question.options), "context": text}
        resp = httpx.post(f"{self.base_url}/predict", json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        probs = resp.json()["probabilities"]
        if isinstance(probs, dict):
            probs = [probs.get(option, 0.0) for option in question.options]
        if len(probs) != len(question.options):
            raise ValueError(f"Jev 回傳 {len(probs)} 個機率，但題目有 {len(question.options)} 個選項")
        return [float(p) for p in probs]

"""Jev（TypeSafe AI 的決策模型，規格書 3.1）：判斷題，不產生文字。

- client.py：正式 API；mock.py：規則模擬版（沒有金鑰時用、測試用）
- fallback.py：信心值不足時改問本機 Ollama
- service.py：先問 Jev、沒把握再問 LLM
- classify.py：寫入時的判斷（電影種類、餐廳類別、所屬區域），存進 jev_judgments
- questions.py：所有題目
"""

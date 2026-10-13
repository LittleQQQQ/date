import anyio

from app import llm
from app.llm import has_numbers


def test_reason_without_numbers_is_accepted():
    assert not has_numbers("深夜酒吧燈光昏暗、適合兩人小酌；想省錢可以選湖景咖啡。")
    assert not has_numbers("十分適合約會，想輕鬆一點可以選一個安靜的咖啡廳。")


def test_reason_with_numbers_is_rejected():
    assert has_numbers("走到影城約需 34 分鐘。")
    assert has_numbers("下午四點二十一分出門。")  # 小模型把 14:21 寫錯成中文數字
    assert has_numbers("每人約八百八十元。")
    assert has_numbers("大約兩小時就能吃完。")


def test_digits_inside_given_names_are_not_numbers():
    names = {"定食8", "粽邪4", "3D"}
    assert not has_numbers("看完《粽邪4》的3D版，再到定食8吃飯，很適合約會。", names)
    assert has_numbers("到定食8吃飯，大約兩小時就能吃完。", names)  # 名稱以外的數字照樣擋
    assert has_numbers("到定食8吃飯。")


PLAN = {
    "label": "Jev 首選",
    "order": "movie_first",
    "order_text": "先看電影再吃飯",
    "flags": [],
    "movie": {"start": "17:50", "cinema": "威尼斯影城", "title": "粽邪4", "label": "水影威尼斯"},
    "restaurant": {"name": "定食8", "cuisine": "日式", "date_fit": "適合約會"},
    "leave_home": "16:29",
    "arrive_home": "22:23",
    "cost_per_person": 860,
    "transit": {"go": {"choice": "公車", "fare": 30}, "back": {"choice": "公車", "fare": 30}},
}


def _summarize_with(monkeypatch, reply):
    prompts = []

    async def fake_chat(messages, **kwargs):
        prompts.append(messages[-1]["content"])
        return reply

    monkeypatch.setattr(llm, "_ollama_chat", fake_chat)
    text, source = anyio.run(llm.summarize, {"plans": [PLAN], "stats": {"feasible": 5}, "notes": []})
    return text, source, prompts


def test_summary_keeps_a_reason_that_quotes_names_with_digits(monkeypatch):
    reason = "看完《粽邪4》再到定食8吃日式，節奏輕鬆，很適合約會。"
    text, source, prompts = _summarize_with(monkeypatch, reason)
    assert source == "llm" and text.endswith(reason)
    assert "（版本：水影威尼斯）" in prompts[0]  # 標明是版本，免得被當成餐廳


def test_summary_drops_a_reason_with_numbers(monkeypatch):
    _, source, _ = _summarize_with(monkeypatch, "到定食8吃飯，大約兩小時就能吃完。")
    assert source == "template"


def test_template_summary_names_the_trips():
    from app.llm import template_summary

    plan = {
        "movie": {"start": "18:10", "cinema": "信義威秀", "title": "新片 A", "label": "IMAX 2D 英語"},
        "restaurant": {"name": "霧光義式餐館"},
        "order_text": "先看電影再吃飯",
        "leave_home": "17:30",
        "arrive_home": "22:10",
        "cost_per_person": 1310,
        "transit": {"go": {"choice": "捷運板南線", "fare": 45}, "back": {"choice": "步行", "fare": 0}},
    }
    text = template_summary({"plans": [plan], "stats": {"feasible": 5}, "notes": []})
    assert "去程搭捷運板南線、回程步行" in text
    assert "每人約 1310 元（含來回車資約 45 元）" in text


def test_template_summary_leaves_out_the_fare_note_when_walking_both_ways():
    from app.llm import template_summary

    plan = {
        "movie": {"start": "18:10", "cinema": "信義威秀", "title": "新片 A", "label": "IMAX 2D 英語"},
        "restaurant": {"name": "霧光義式餐館"},
        "order_text": "先看電影再吃飯",
        "leave_home": "17:30",
        "arrive_home": "22:10",
        "cost_per_person": 1250,
        "transit": {"go": {"choice": "步行", "fare": 0}, "back": {"choice": "步行", "fare": 0}},
    }
    text = template_summary({"plans": [plan], "stats": {"feasible": 5}, "notes": []})
    assert "去程步行、回程步行" in text
    assert "含來回車資" not in text
    assert text.endswith("每人約 1250 元。")

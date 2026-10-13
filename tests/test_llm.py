from app.llm import has_numbers


def test_reason_without_numbers_is_accepted():
    assert not has_numbers("深夜酒吧燈光昏暗、適合兩人小酌；想省錢可以選湖景咖啡。")
    assert not has_numbers("十分適合約會，想輕鬆一點可以選一個安靜的咖啡廳。")


def test_reason_with_numbers_is_rejected():
    assert has_numbers("走到影城約需 34 分鐘。")
    assert has_numbers("下午四點二十一分出門。")  # 小模型把 14:21 寫錯成中文數字
    assert has_numbers("每人約八百八十元。")
    assert has_numbers("大約兩小時就能吃完。")


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

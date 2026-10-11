from app.llm import has_numbers


def test_reason_without_numbers_is_accepted():
    assert not has_numbers("深夜酒吧燈光昏暗、適合兩人小酌；想省錢可以選湖景咖啡。")
    assert not has_numbers("十分適合約會，想輕鬆一點可以選一個安靜的咖啡廳。")


def test_reason_with_numbers_is_rejected():
    assert has_numbers("走到影城約需 34 分鐘。")
    assert has_numbers("下午四點二十一分出門。")  # 小模型把 14:21 寫錯成中文數字
    assert has_numbers("每人約八百八十元。")
    assert has_numbers("大約兩小時就能吃完。")

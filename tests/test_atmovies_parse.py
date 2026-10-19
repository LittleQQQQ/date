import datetime as dt
from pathlib import Path

from app.collectors.atmovies import (
    normalize_time,
    parse_map,
    parse_region,
    parse_synopsis,
    parse_theater_day,
    theater_url,
)

SAMPLES = Path(__file__).parent / "samples" / "atmovies"


def read(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def test_parse_region_lists_each_theater_once():
    theaters = parse_region(read("region_a02.html"), "a02")
    assert [t.tid for t in theaters] == ["t02a01", "t02a03", "t02b01", "t02c09"]
    assert theaters[0].name == "台北信義威秀"


def test_parse_map():
    assert parse_map(read("map_t02a01.html")) == (25.035684, 121.566542)
    assert parse_map("<html>沒有地圖</html>") is None


def test_parse_theater_day():
    page = parse_theater_day(read("theater_t02a01_20261012.html"))
    assert page.date == dt.date(2026, 10, 12)
    assert page.address == "台北市信義區松壽路16、18號"
    films = {(f.movie_id, f.version): f for f in page.films}
    sinner = films[("fven32261958", "")]
    assert sinner.title == "不存在的罪人" and sinner.runtime_min == 117
    assert sinner.times == ["19:00", "21:15", "23:30", "24:40"]
    assert films[("fsjp92458256", "國語版")].times == ["14:45", "18:05"]
    assert films[("fsjp92458256", "日語版")].times == ["20:10"]
    assert films[("fren35538033", "4DX版")].runtime_min == 110
    assert films[("fyjp43723335", "")].runtime_min is None
    assert "特別放映活動" not in {f.title for f in page.films}  # 沒有電影頁的活動跳過


def test_normalize_time():
    assert normalize_time("10：20") == "10:20"
    assert normalize_time("0:15") == "24:15"
    assert normalize_time("其他戲院(90)") is None


def test_parse_synopsis():
    text = parse_synopsis(read("movie_fven32261958.html"))
    assert text.startswith("代筆作家受聘前往偏遠宅邸")
    assert "《另一部片》" in text
    assert "<" not in text and "劇情簡介" not in text and "START" not in text
    assert text.endswith("她開始懷疑眼前的一切。")
    assert parse_synopsis("<html></html>") == ""


def test_theater_url():
    today = dt.date(2026, 10, 12)
    assert theater_url("t02a01", "a02", today, today) == "https://www.atmovies.com.tw/showtime/t02a01/a02/"
    assert theater_url("t02a01", "a02", dt.date(2026, 10, 13), today).endswith("/showtime/t02a01/a02/20261013/")

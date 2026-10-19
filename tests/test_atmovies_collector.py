"""開眼爬蟲：用存下來的網頁（台北信義一帶的樣本），不連網。"""
import datetime as dt

import pytest
from fakes import SAMPLES as ALL_SAMPLES
from fakes import FakeFetcher
from sqlalchemy import func, select

from app.collectors.atmovies import MAP_URL, AtmoviesCollector, movie_url, region_url, show_time, theater_url
from app.collectors.base import ScrapeError
from app.collectors.coverage import Coverage, CoveredStation
from app.ingest import run_collector
from app.models import Cinema, Movie, Showtime
from app.timeutil import TW

SAMPLES = ALL_SAMPLES / "atmovies"
REGION = "a02"  # 樣本是台北的網頁
TODAY = dt.date(2026, 10, 12)
TOMORROW = TODAY + dt.timedelta(days=1)
COV = Coverage([
    CoveredStation("tpe-cityhall", "市政府", "xinyi", 25.0410, 121.5655),
    CoveredStation("tpe-taipei101", "台北101", "xinyi", 25.0330, 121.5637),
    CoveredStation("tpe-ximen", "西門", "ximen", 25.0421, 121.5081),  # 國賓（t02b01）在這一帶
])


def pages():
    s = SAMPLES
    return {
        region_url(REGION): s / "region_a02.html",
        MAP_URL.format(tid="t02a01"): s / "map_t02a01.html",
        MAP_URL.format(tid="t02a03"): s / "map_t02a03.html",
        MAP_URL.format(tid="t02b01"): s / "map_t02b01.html",
        MAP_URL.format(tid="t02c09"): s / "map_t02c09.html",
        theater_url("t02a01", REGION, TODAY, TODAY): s / "theater_t02a01_20261012.html",
        theater_url("t02a01", REGION, TOMORROW, TODAY): s / "theater_t02a01_20261013.html",
        theater_url("t02a03", REGION, TODAY, TODAY): s / "theater_small_20261012.html",
        theater_url("t02a03", REGION, TOMORROW, TODAY): s / "theater_small_20261013.html",
        theater_url("t02b01", REGION, TODAY, TODAY): s / "theater_small_20261012.html",
        # 國賓隔天的時刻表故意不提供：模擬抓失敗
        movie_url("fven32261958"): s / "movie_fven32261958.html",
        # 另外兩部片的電影頁也不提供：簡介留空
    }


def empty_day(day):
    """有日期、沒有任何電影的時刻表頁。"""
    return f"<html><body><h3>{day:%Y/%m/%d} (一)</h3></body></html>"


def collector(days=2, coverage=COV, **overrides):
    """overrides：網址 → 內容，蓋掉 pages() 裡同一個網址（用來模擬某一頁壞掉）。"""
    fetcher = FakeFetcher({**pages(), **overrides})
    return AtmoviesCollector(coverage, fetcher=fetcher, today=TODAY, region=REGION, days=days), fetcher


def collect(days=2, **overrides):
    c, fetcher = collector(days, **overrides)
    return c.collect(), fetcher


def test_only_theaters_within_walking_distance_are_kept():
    batch, fetcher = collect()
    assert [c.atmovies_id for c in batch.cinemas] == ["t02a01", "t02a03"]
    assert theater_url("t02c09", REGION, TODAY, TODAY) not in fetcher.requested  # 範圍外的影城不抓時刻表
    xinyi = batch.cinemas[0]
    assert xinyi.address == "台北市信義區松壽路16、18號" and xinyi.lat == 25.035684


def test_failed_theater_keeps_old_showtimes():
    batch, _ = collect()
    assert batch.showtime_cinemas == ["t02a01", "t02a03"]
    assert all(s.cinema_atmovies_id != "t02b01" for s in batch.showtimes)
    assert batch.showtime_window == (show_time(TODAY, "00:00"), show_time(TOMORROW, "29:00"))
    assert batch.stats["failed_pages"] == 3  # 國賓隔天的時刻表＋兩部片的電影頁


def test_showtimes_are_points_in_time():
    batch, _ = collect()
    late = next(s for s in batch.showtimes if s.cinema_atmovies_id == "t02a01" and s.starts_at.hour == 0)
    # 網站寫 00:40 的場次算前一天的深夜場：10/12 的 24:40 就是 10/13 凌晨 00:40
    assert late.starts_at == dt.datetime(2026, 10, 13, 0, 40, tzinfo=TW)
    assert late.ends_at - late.starts_at == dt.timedelta(minutes=117)  # 片長
    assert late.version == "數位" and late.movie_atmovies_id == "fven32261958"
    assert any(s.version == "4DX版" for s in batch.showtimes)
    assert batch.stats["showtimes_skipped_no_runtime"] == 1


def test_movies_and_synopsis():
    batch, _ = collect()
    movies = {m.atmovies_id: m for m in batch.movies}
    assert set(movies) == {"fven32261958", "fsjp92458256", "fren35538033"}
    assert movies["fven32261958"].runtime_min == 117
    assert movies["fven32261958"].synopsis.startswith("代筆作家")
    assert movies["fsjp92458256"].synopsis == ""


def test_malformed_timetable_page_skips_only_that_theater():
    bad = "<html><body><h3>2026/13/45 (二)</h3></body></html>"  # 不存在的日期
    batch, _ = collect(**{theater_url("t02a03", REGION, TOMORROW, TODAY): bad})
    assert [c.atmovies_id for c in batch.cinemas] == ["t02a01"]
    assert batch.stats["failed_pages"] == 4


def test_unparseable_map_page_is_skipped_and_not_cached():
    map_url = MAP_URL.format(tid="t02a03")
    batch, fetcher = collect(**{map_url: "<html>系統忙碌中，請稍後再試</html>"})
    assert [c.atmovies_id for c in batch.cinemas] == ["t02a01"]
    assert fetcher.forgotten == [map_url]  # 壞掉的地圖頁不能留在快取裡 30 天


def test_extra_days_stop_at_the_first_missing_page():
    # 前 4 天要全部抓得到（沒有場次的頁面就好）；第 5 天抓不到就停，第 6、7 天不抓，前 4 天照用
    later = {theater_url("t02a01", REGION, TODAY + dt.timedelta(days=i), TODAY): empty_day(TODAY + dt.timedelta(days=i))
             for i in (2, 3)}
    batch, fetcher = collect(days=7, **later)
    assert [c.atmovies_id for c in batch.cinemas] == ["t02a01"]  # 另外兩家前 4 天有缺頁，保留上次的場次
    assert any("20261016" in url for url in fetcher.requested)
    assert not any("20261017" in url for url in fetcher.requested)


def test_region_page_without_theaters_fails():
    with pytest.raises(ScrapeError, match="改版"):
        collect(**{region_url(REGION): "<html>改版了</html>"})


def test_region_without_theaters_in_any_area_is_a_normal_empty_result():
    far = Coverage([CoveredStation("far", "遠方", "x", 25.2000, 121.9000)])
    c, _ = collector(coverage=far)
    batch = c.collect()
    assert batch.cinemas == [] and batch.showtimes == []  # 範圍附近沒有影城：正常的空結果，不是錯誤


def test_all_theaters_in_range_failing_is_an_error():
    broken = {theater_url(t, REGION, d, TODAY): None for t in ("t02a01", "t02a03", "t02b01") for d in (TODAY, TOMORROW)}
    with pytest.raises(ScrapeError, match="全部抓取失敗"):
        collect(**broken)


def test_run_atmovies_into_the_database(session):
    """實際寫進資料庫：開演時間要晚於現在，所以把「今天」設成明天（樣本網頁的日期也一起換）。"""
    today = dt.datetime.now(TW).date() + dt.timedelta(days=1)

    def shifted(sample, day):
        return (SAMPLES / sample).read_text(encoding="utf-8").replace("2026/10/12", f"{day:%Y/%m/%d}")

    overrides = {theater_url("t02a01", REGION, today, today): shifted("theater_t02a01_20261012.html", today),
                 theater_url("t02a03", REGION, today, today): shifted("theater_small_20261012.html", today)}
    fetcher = FakeFetcher({**pages(), **overrides})
    run = run_collector(session, AtmoviesCollector(COV, fetcher=fetcher, today=today, region=REGION, days=1))
    assert run.status == "success", run.error
    assert run.stats["cinemas"] == 2 and run.stats["showtimes"] > 0
    assert session.scalar(select(func.count()).select_from(Showtime)) == run.stats["showtimes"]
    assert session.scalar(select(func.min(Showtime.starts_at))).date() >= today
    assert session.scalar(select(Movie.runtime_min).where(Movie.atmovies_id == "fven32261958")) == 117
    assert {p for p in session.scalars(select(Cinema.ticket_price))} == {300}  # 套用 data/seed 的預設票價

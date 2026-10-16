import datetime as dt

import pytest
from fakes import SAMPLES as ALL_SAMPLES
from fakes import FakeFetcher
from sqlalchemy import select

from app.collectors.atmovies import MAP_URL, AtmoviesCollector, movie_url, region_url, theater_url
from app.collectors.base import ScrapeError
from app.collectors.coverage import AreaCoverage, Coverage, Point, load_coverage
from app.collectors.fixture import BaseCollector
from app.ingest import run_collector
from app.models import Cinema, Showtime

SAMPLES = ALL_SAMPLES / "atmovies"
TODAY = dt.date(2026, 10, 12)
TOMORROW = TODAY + dt.timedelta(days=1)
COV = Coverage([
    AreaCoverage("台北市/信義", "台北市", [Point(25.0410, 121.5655), Point(25.0330, 121.5637)]),
    AreaCoverage("台北市/西門町", "台北市", [Point(25.0421, 121.5081)]),
])


def pages():
    s = SAMPLES
    return {
        region_url("a02"): s / "region_a02.html",
        MAP_URL.format(tid="t02a01"): s / "map_t02a01.html",
        MAP_URL.format(tid="t02a03"): s / "map_t02a03.html",
        MAP_URL.format(tid="t02b01"): s / "map_t02b01.html",
        MAP_URL.format(tid="t02c09"): s / "map_t02c09.html",
        theater_url("t02a01", "a02", TODAY, TODAY): s / "theater_t02a01_20261012.html",
        theater_url("t02a01", "a02", TOMORROW, TODAY): s / "theater_t02a01_20261013.html",
        theater_url("t02a03", "a02", TODAY, TODAY): s / "theater_small_20261012.html",
        theater_url("t02a03", "a02", TOMORROW, TODAY): s / "theater_small_20261013.html",
        theater_url("t02b01", "a02", TODAY, TODAY): s / "theater_small_20261012.html",
        # 國賓隔天的時刻表故意不提供：模擬抓失敗
        movie_url("fven32261958"): s / "movie_fven32261958.html",
        # 另外兩部片的電影頁也不提供：簡介留空
    }


def empty_day(day):
    """有日期、沒有任何電影的時刻表頁。"""
    return f"<html><body><h3>{day:%Y/%m/%d} (一)</h3></body></html>"


def collect(days=2, **overrides):
    """overrides：網址 → 內容，蓋掉 pages() 裡同一個網址（用來模擬某一頁壞掉）。"""
    fetcher = FakeFetcher({**pages(), **overrides})
    batch = AtmoviesCollector(COV, fetcher=fetcher, today=TODAY).collect(TODAY, days)
    return batch, fetcher


def test_only_theaters_in_areas_are_kept():
    batch, fetcher = collect()
    assert {c.external_id: c.area_id for c in batch.cinemas} == {"t02a01": "台北市/信義", "t02a03": "台北市/信義"}
    assert theater_url("t02c09", "a02", TODAY, TODAY) not in fetcher.requested  # 範圍外的影城不抓時刻表
    xinyi = next(c for c in batch.cinemas if c.external_id == "t02a01")
    assert xinyi.address == "台北市信義區松壽路16、18號" and xinyi.lat == 25.035684


def test_failed_theater_keeps_old_showtimes():
    batch, _ = collect()
    assert batch.showtime_cinemas == ["t02a01", "t02a03"]
    assert all(s.cinema_external_id != "t02b01" for s in batch.showtimes)
    assert batch.showtime_dates == (TODAY, TOMORROW)
    assert batch.stats["failed_pages"] == 3  # 國賓隔天的時刻表＋兩部片的電影頁


def test_malformed_timetable_page_skips_only_that_theater():
    bad = "<html><body><h3>2026/13/45 (二)</h3></body></html>"  # 不存在的日期
    batch, _ = collect(**{theater_url("t02a03", "a02", TOMORROW, TODAY): bad})
    assert [c.external_id for c in batch.cinemas] == ["t02a01"]
    assert batch.showtime_cinemas == ["t02a01"]
    assert all(s.cinema_external_id == "t02a01" for s in batch.showtimes) and batch.showtimes
    assert batch.stats["failed_pages"] == 4  # 原本的 3 頁＋這一頁


def test_timetable_page_with_wrong_date_skips_that_theater():
    wrong_day = SAMPLES / "theater_small_20261012.html"  # 抓隔天卻拿到今天的頁面
    batch, _ = collect(**{theater_url("t02a03", "a02", TOMORROW, TODAY): wrong_day})
    assert [c.external_id for c in batch.cinemas] == ["t02a01"]
    assert batch.showtime_cinemas == ["t02a01"]
    assert batch.stats["failed_pages"] == 4


def test_unparseable_map_page_is_skipped_and_not_cached():
    busy = "<html>系統忙碌中，請稍後再試</html>"
    map_url = MAP_URL.format(tid="t02a03")
    batch, fetcher = collect(**{map_url: busy})
    assert [c.external_id for c in batch.cinemas] == ["t02a01"]
    assert batch.stats["failed_pages"] == 4
    assert fetcher.forgotten == [map_url]  # 壞掉的地圖頁不能留在快取裡 30 天


def test_showtime_fields():
    batch, _ = collect()
    late = next(s for s in batch.showtimes if s.cinema_external_id == "t02a01" and s.start == "24:40")
    assert late.date == TODAY and late.label == "數位" and late.price == 300 and late.estimated == ["price"]
    assert late.movie_external_id == "fven32261958"
    assert next(s for s in batch.showtimes if s.label == "4DX版").price == 420
    assert not any(s.raw_title.startswith("YORUSHIKA") for s in batch.showtimes)
    assert batch.stats["showtimes_skipped_no_runtime"] == 1


def test_movies_and_synopsis():
    batch, _ = collect()
    movies = {m.external_id: m for m in batch.movies}
    assert set(movies) == {"fven32261958", "fsjp92458256", "fren35538033"}
    assert movies["fven32261958"].runtime_min == 117
    assert movies["fven32261958"].synopsis.startswith("代筆作家")
    assert movies["fsjp92458256"].synopsis == ""
    assert all(m.genre == "" for m in movies.values())


def days_from_today(*offsets):
    return [TODAY + dt.timedelta(days=i) for i in offsets]


def published_day(day):
    """有場次的時刻表頁：拿 10/13 的樣本換成 day 的日期。"""
    return (SAMPLES / "theater_t02a01_20261013.html").read_text(encoding="utf-8").replace("2026/10/13", f"{day:%Y/%m/%d}")


def test_extra_days_stop_at_the_first_missing_page():
    # 前 4 天要全部抓得到（沒有場次的頁面就好）；第 5 天抓不到就停，第 6、7 天不抓，前 4 天照用
    later = {theater_url("t02a01", "a02", day, TODAY): empty_day(day) for day in days_from_today(2, 3)}
    batch, fetcher = collect(days=14, **later)
    assert [c.external_id for c in batch.cinemas] == ["t02a01"]  # 另外兩家前 4 天有缺頁，保留上次的場次
    assert batch.showtime_dates == (TODAY, TODAY + dt.timedelta(days=6))  # 最多 7 天
    assert any("20261016" in url for url in fetcher.requested)
    assert not any("20261017" in url for url in fetcher.requested)


def test_published_extra_days_are_kept_until_an_empty_day():
    pages_by_day = {2: empty_day, 3: empty_day, 4: published_day, 5: published_day, 6: empty_day}  # 第 7 天還沒公布
    later = {theater_url("t02a01", "a02", day, TODAY): pages_by_day[(day - TODAY).days](day) for day in days_from_today(*pages_by_day)}
    batch, fetcher = collect(days=14, **later)
    dates = {s.date for s in batch.showtimes if s.cinema_external_id == "t02a01"}
    assert dates == set(days_from_today(0, 1, 4, 5))
    assert not any("20261019" in url for url in fetcher.requested)  # 第 8 天不抓


def test_extra_day_with_the_wrong_date_stops_without_dropping_the_theater():
    later = {theater_url("t02a01", "a02", day, TODAY): empty_day(day) for day in days_from_today(2, 3)}
    later[theater_url("t02a01", "a02", TODAY + dt.timedelta(days=4), TODAY)] = published_day(TODAY)  # 拿到今天的頁面
    batch, fetcher = collect(days=14, **later)
    assert [c.external_id for c in batch.cinemas] == ["t02a01"]
    assert {s.date for s in batch.showtimes} == {TODAY, TOMORROW}
    assert not any("20261017" in url for url in fetcher.requested)


def test_region_page_without_theaters_fails():
    fetcher = FakeFetcher({region_url("a02"): "<html>改版了</html>"})
    with pytest.raises(ScrapeError, match="改版"):
        AtmoviesCollector(COV, fetcher=fetcher, today=TODAY).collect(TODAY, 2)


def test_run_atmovies_into_database(mem_session):
    run_collector(mem_session, BaseCollector(), judge=False)
    taipei = Coverage([a for a in load_coverage(mem_session).areas if a.city == "台北市"])
    collector = AtmoviesCollector(taipei, fetcher=FakeFetcher(pages()), today=TODAY)
    run = run_collector(mem_session, collector, start=TODAY, days=2)
    assert run.status == "ok", run.error
    assert run.counts["failed_pages"] == 3
    rows = mem_session.execute(
        select(Showtime, Cinema).join(Cinema, Showtime.cinema_id == Cinema.id).where(Showtime.source == "atmovies")
    ).all()
    assert rows and all(c.area_id == "台北市/信義" for _, c in rows)
    assert all(s.movie_id is not None for s, _ in rows)


def _theater_pages(tids, make_page):
    """每家影城今天、明天的時刻表都換成 make_page(日期) 的內容；內容是 None 就是抓不到。"""
    return {
        theater_url(tid, "a02", day, TODAY): make_page(day) for tid in tids for day in (TODAY, TOMORROW)
    }


def test_all_in_area_theaters_failing_is_an_error():
    broken = _theater_pages(["t02a01", "t02a03", "t02b01"], lambda day: None)  # 商圈內的影城時刻表全部抓不到
    with pytest.raises(ScrapeError, match="全部抓取失敗"):
        collect(**broken)


def test_region_without_theaters_in_any_area_is_a_normal_empty_result():
    far = Coverage([AreaCoverage("台北市/遠方", "台北市", [Point(25.2000, 121.9000)])])
    batch = AtmoviesCollector(far, fetcher=FakeFetcher(pages()), today=TODAY).collect(TODAY, 2)
    assert batch.cinemas == [] and batch.showtimes == []  # 商圈附近沒有影城：正常的空結果，不是錯誤


def test_timetables_without_any_showtime_are_an_error():
    with pytest.raises(ScrapeError, match="沒有場次"):
        collect(**_theater_pages(["t02a01", "t02a03", "t02b01"], empty_day))


def test_movie_page_without_synopsis_is_not_kept_in_the_cache():
    url = movie_url("fsjp92458256")
    batch, fetcher = collect(**{url: "<html>系統忙碌中，請稍後再試</html>"})
    movies = {m.external_id: m for m in batch.movies}
    assert movies["fsjp92458256"].synopsis == ""
    assert fetcher.forgotten == [url]  # 壞掉的電影頁不能留在快取裡 30 天；有簡介的頁面照常快取


def test_long_text_is_cut_to_the_column_lengths():
    def long_page(day):
        return (
            f"<html><body><ul><li>地址: {'路' * 250}\n<a>(地圖)</a></ul><h3>{day:%Y/%m/%d} (一)</h3>"
            f'<ul id="theaterShowtimeTable"><li class="filmTitle"><a href="/movie/fven32261958/">{"片" * 120}</a></li>'
            f'<li><ul><li>片長：100分</li></ul><ul><li class="filmVersion">{"版" * 60}</li><li>19：00</li></ul></li></ul>'
            "</body></html>"
        )

    region = f'<html><body><a href="/showtime/t02a01/a02/">{"影" * 60}</a></body></html>'
    batch, _ = collect(**{region_url("a02"): region}, **_theater_pages(["t02a01"], long_page))
    cinema, movie, showtime = batch.cinemas[0], batch.movies[0], batch.showtimes[0]
    assert (len(cinema.name), len(cinema.address)) == (50, 200)
    assert len(movie.title) == 100
    assert (len(showtime.raw_title), len(showtime.label)) == (100, 50)

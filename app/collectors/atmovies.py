"""開眼電影網爬蟲：影城、電影、場次（規格書 7.2 refresh_daily）。

只抓桃園市（地區代碼 a03），而且只留走得到 4 個區域車站的影城（見 coverage.py）。
- 地區頁 /showtime/{地區}/              → 影城清單
- 地圖頁 GMap2018.cfm?tid={影城}         → 經緯度（快取 30 天）。影城的座標直接用這裡的，不需要地理編碼
- 時刻表 /showtime/{影城}/{地區}/[日期]/ → 地址、每部片的片長、版本、場次（今天起最多 7 天）
- 電影頁 /movie/{電影}/                  → 劇情簡介（快取 30 天），給 Jev 判斷電影種類
網站沒有票價：票價是每家影城手動設定的（data/seed/ticket_prices.json）。影城屬於哪個區域、電影種類由 Jev 判斷。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Comment
from sqlalchemy.orm import Session

from ..timeutil import TW, today_tw
from .base import Batch, CinemaRecord, Collector, MovieRecord, ScrapeError, ShowtimeRecord
from .coverage import Coverage, load_coverage
from .http import Fetcher, FetchError

BASE_URL = "https://www.atmovies.com.tw"
MAP_URL = "http://ww2.atmovies.com.tw/movie/GMap2018.cfm?tid={tid}"
REGION = "a03"  # 桃園市
# 影城每週公布一次下一週的場次，網站上查得到的天數會變（週初常常只到週四，公布後可以到 6 天以上）
SITE_DAYS = 7  # 最多抓今天起幾天
REQUIRED_DAYS = 4  # 前 4 天任何一頁抓失敗，這家影城就保留上次的場次；之後的天數遇到抓不到或還沒公布就停
CACHE_DAYS = 30  # 地圖頁、電影頁很少變
SYNOPSIS_MAX = 600
DEFAULT_VERSION = "數位"  # 沒標版本的場次


@dataclass(frozen=True)
class TheaterLink:
    tid: str
    name: str


@dataclass
class FilmShowing:
    movie_id: str
    title: str
    runtime_min: int | None  # 抓不到片長時為 None，這部片的場次會跳過
    version: str  # 「國語版」「4DX版」；沒標為空字串
    times: list[str] = field(default_factory=list)


@dataclass
class TheaterDay:
    date: dt.date | None
    address: str
    films: list[FilmShowing]


_THEATER_HREF = re.compile(r"^/showtime/(t\w+)/(a\d+)/$")
_COORDS = re.compile(r"lat:\s*(-?\d+(?:\.\d+)?)\s*,\s*lng:\s*(-?\d+(?:\.\d+)?)")
_DATE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
_ADDRESS = re.compile(r"地址\s*[:：]\s*([^<\n(]+)")
_MOVIE_HREF = re.compile(r"^/movie/([^/]+)/$")
_RUNTIME = re.compile(r"片長：\s*(\d+)\s*分")
_TIME = re.compile(r"^(\d{1,2})[：:](\d{2})$")


def region_url(region: str) -> str:
    return f"{BASE_URL}/showtime/{region}/"


def theater_url(tid: str, region: str, day: dt.date, today: dt.date) -> str:
    base = f"{BASE_URL}/showtime/{tid}/{region}/"
    return base if day == today else f"{base}{day:%Y%m%d}/"


def movie_url(movie_id: str) -> str:
    return f"{BASE_URL}/movie/{movie_id}/"


def parse_region(html: str, region: str) -> list[TheaterLink]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, TheaterLink] = {}
    for a in soup.find_all("a", href=True):
        m = _THEATER_HREF.match(a["href"])
        if m and m.group(2) == region and m.group(1) not in found:
            found[m.group(1)] = TheaterLink(m.group(1), a.get_text(strip=True))
    return list(found.values())


def parse_map(html: str) -> tuple[float, float] | None:
    m = _COORDS.search(html)
    return (float(m.group(1)), float(m.group(2))) if m else None


def normalize_time(text: str) -> str | None:
    """「10：20」→「10:20」；凌晨的場次算前一天的深夜場，「00：40」→「24:40」。"""
    m = _TIME.match(text.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour < 5:
        hour += 24
    return f"{hour:02d}:{minute:02d}"


def parse_theater_day(html: str) -> TheaterDay:
    soup = BeautifulSoup(html, "html.parser")
    day = None
    for h3 in soup.find_all("h3"):
        if m := _DATE.search(h3.get_text()):
            day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            break
    # 地址那一行的 <li> 在原始網頁裡沒有關閉，直接從原始碼抓
    address = m.group(1).strip() if (m := _ADDRESS.search(html)) else ""

    films = []
    for block in soup.select("ul#theaterShowtimeTable"):
        link = block.select_one("li.filmTitle a[href]")
        movie = _MOVIE_HREF.match(link["href"]) if link else None
        if not movie:
            continue  # 沒有電影頁的特別活動，不知道片長
        runtime = _RUNTIME.search(block.get_text())
        minutes = int(runtime.group(1)) if runtime else 0
        version = block.select_one("li.filmVersion")
        times = [t for li in block.find_all("li") if (t := normalize_time(li.get_text(strip=True)))]
        films.append(
            FilmShowing(
                movie_id=movie.group(1),
                title=link.get_text(strip=True),
                runtime_min=minutes or None,
                version=version.get_text(strip=True) if version else "",
                times=times,
            )
        )
    return TheaterDay(day, address, films)


def parse_synopsis(html: str) -> str:
    start, end = html.find("<!-- Story info start -->"), html.find("<!-- Story info end -->")
    if start < 0 or end < start:
        return ""
    soup = BeautifulSoup(html[start:end], "html.parser")
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = re.sub(r"\s+", " ", soup.get_text()).strip()
    return text.removeprefix("劇情簡介").strip()[:SYNOPSIS_MAX]


def show_time(day: dt.date, clock: str) -> dt.datetime:
    """場次的開演時間點。clock 是 normalize_time 的結果：「24:40」是隔天凌晨 00:40。"""
    hour, minute = map(int, clock.split(":"))
    return dt.datetime(day.year, day.month, day.day, tzinfo=TW) + dt.timedelta(hours=hour, minutes=minute)


class AtmoviesCollector(Collector):
    name = "atmovies"

    def __init__(
        self, coverage: Coverage, fetcher: Fetcher | None = None, today: dt.date | None = None,
        region: str = REGION, days: int = SITE_DAYS,
    ):
        self.coverage = coverage
        self.fetcher = fetcher or Fetcher()
        self.today = today
        self.region = region
        self.days = days  # 抓今天起幾天（測試可以少抓幾天）

    @classmethod
    def create(cls, session: Session) -> AtmoviesCollector:
        return cls(load_coverage(session))

    def collect(self) -> Batch:
        today = self.today or today_tw()
        dates = [today + dt.timedelta(days=i) for i in range(self.days)]
        theaters = parse_region(self.fetcher.get(region_url(self.region)), self.region)
        if not theaters:
            raise ScrapeError(f"開眼地區頁 {self.region} 解析不到影城，網站可能改版了")

        self.failed = self.skipped = self.attempted = 0
        cinemas: list[CinemaRecord] = []
        films: dict[str, FilmShowing] = {}  # 電影編號 → 第一次看到的片名、片長
        showtimes: list[ShowtimeRecord] = []
        for theater in theaters:
            result = self._theater(theater, dates, today)
            if result is None:
                continue
            cinema, theater_films, theater_showtimes = result
            cinemas.append(cinema)
            for film in theater_films:
                films.setdefault(film.movie_id, film)
            showtimes += theater_showtimes

        if not cinemas and self.attempted:
            raise ScrapeError("開眼：區域內的影城全部抓取失敗")
        if cinemas and not showtimes:
            raise ScrapeError("開眼：影城頁面都沒有場次，網站可能改版了")

        # 這幾天的場次（含跨午夜到凌晨 5 點的深夜場）由這批取代；抓失敗的影城不列入，保留上次的場次
        window = (show_time(dates[0], "00:00"), show_time(dates[-1], "29:00"))
        return Batch(
            cinemas=cinemas,
            movies=[self._movie(film) for film in films.values()],
            showtimes=showtimes,
            showtime_cinemas=[c.atmovies_id for c in cinemas],
            showtime_window=window,
            stats={"failed_pages": self.failed, "showtimes_skipped_no_runtime": self.skipped},
        )

    def _theater(
        self, theater: TheaterLink, dates: list[dt.date], today: dt.date
    ) -> tuple[CinemaRecord, list[FilmShowing], list[ShowtimeRecord]] | None:
        """抓一家影城。不在區域範圍、或前 REQUIRED_DAYS 天有一頁抓失敗就回傳 None（保留這家影城上次的場次）。

        之後的天數是加抓的：抓不到、日期不對就停在那裡，還沒公布（沒有場次）的那天之後也不再抓，前面抓到的照用。
        self.attempted 記有多少家影城在範圍內（或地圖抓不到、不知道在不在）而試著抓過，
        用來分辨「範圍內的影城全部失敗」和「範圍附近本來就沒有影城」。
        """
        map_url = MAP_URL.format(tid=theater.tid)
        point = None
        try:
            point = parse_map(self.fetcher.get(map_url, cache_days=CACHE_DAYS))
            if point is None:
                self.fetcher.forget(map_url)  # 網站忙碌時可能回 200 的空頁面，不能讓它被快取 30 天
        except FetchError:
            pass
        if point is None:
            self.failed += 1
            self.attempted += 1
            return None
        if self.coverage.nearest(*point) is None:
            return None
        self.attempted += 1

        pages = []
        for day in dates:
            url = theater_url(theater.tid, self.region, day, today)
            required = (day - today).days < REQUIRED_DAYS
            try:
                page = parse_theater_day(self.fetcher.get(url))
            except (FetchError, ValueError):  # 抓不到、或頁面壞掉（例如日期不存在）
                page = None
            if page is None or page.date != day:  # 日期不對：網站正在換日或頁面不對
                self.failed += 1
                if required:
                    return None  # 只跳過這家，這次先不更新
                break
            pages.append((day, page))
            if not required and not page.films:
                break  # 這天的場次還沒公布，後面的天數也不會有

        cinema = CinemaRecord(atmovies_id=theater.tid, name=theater.name, address=pages[0][1].address,
                              lat=point[0], lng=point[1])
        films: list[FilmShowing] = []
        showtimes: dict[tuple, ShowtimeRecord] = {}
        for day, page in pages:
            for film in page.films:
                if film.runtime_min is None:
                    self.skipped += len(film.times)
                    continue
                films.append(film)
                version = film.version or DEFAULT_VERSION
                for clock in film.times:
                    starts = show_time(day, clock)
                    showtimes[(film.movie_id, starts, version)] = ShowtimeRecord(
                        cinema_atmovies_id=theater.tid, movie_atmovies_id=film.movie_id, starts_at=starts,
                        ends_at=starts + dt.timedelta(minutes=film.runtime_min), version=version,
                    )
        return cinema, films, list(showtimes.values())

    def _movie(self, film: FilmShowing) -> MovieRecord:
        url = movie_url(film.movie_id)
        try:
            synopsis = parse_synopsis(self.fetcher.get(url, cache_days=CACHE_DAYS))
            if not synopsis:
                self.fetcher.forget(url)  # 網站忙碌時可能回 200 的空頁面，不能讓它被快取 30 天
        except FetchError:
            self.failed += 1
            synopsis = ""
        return MovieRecord(atmovies_id=film.movie_id, title=film.title, runtime_min=film.runtime_min, synopsis=synopsis)

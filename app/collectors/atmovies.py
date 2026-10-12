"""開眼電影網爬蟲：影城、電影、場次。

一個網站涵蓋全台幾乎所有影城。只抓有商圈的縣市，而且只留走得到商圈車站的影城。
- 地區頁 /showtime/{地區}/              → 影城清單
- 地圖頁 GMap2018.cfm?tid={影城}         → 經緯度（快取 30 天）
- 時刻表 /showtime/{影城}/{地區}/[日期]/ → 地址、每部片的片長、版本、場次（今天起 4 天）
- 電影頁 /movie/{電影}/                  → 劇情簡介（快取 30 天）
網站沒有票價和片型：票價依版本估計（標成估計），片型交給 Jev 判斷。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Comment

from ..timeutil import today_tw
from .base import Batch, CinemaRecord, Collector, MovieRecord, ScrapeError, ShowtimeRecord
from .coverage import Coverage
from .http import Fetcher, FetchError

BASE_URL = "https://www.atmovies.com.tw"
MAP_URL = "http://ww2.atmovies.com.tw/movie/GMap2018.cfm?tid={tid}"
SITE_DAYS = 4  # 網站提供今天起幾天的時刻表
CACHE_DAYS = 30  # 地圖頁、電影頁很少變
SYNOPSIS_MAX = 600
NAME_MAX, ADDRESS_MAX, TITLE_MAX, LABEL_MAX = 50, 200, 100, 50  # 資料表欄位的長度（models.py），Postgres 超過會整批失敗
DEFAULT_LABEL = "數位"  # 沒標版本的場次
PREMIUM_WORDS = ("IMAX", "4DX", "巨幕", "TITAN", "SCREENX", "DOLBY")
PRICE_PREMIUM, PRICE_NORMAL = 420, 300

# 縣市 → 開眼的地區代碼（2026-10-12 從網站確認；連江縣沒有）
REGION_BY_CITY = {
    "基隆市": "a01", "台北市": "a02", "新北市": "a02", "桃園市": "a03",
    "新竹市": "a35", "新竹縣": "a35", "苗栗縣": "a37", "台中市": "a04",
    "彰化縣": "a47", "雲林縣": "a45", "南投縣": "a49", "嘉義市": "a05",
    "嘉義縣": "a05", "台南市": "a06", "高雄市": "a07", "屏東縣": "a87",
    "宜蘭縣": "a39", "花蓮縣": "a38", "台東縣": "a89", "澎湖縣": "a69",
    "金門縣": "a68",
}


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


def price_for(label: str) -> int:
    return PRICE_PREMIUM if any(w in label.upper() for w in PREMIUM_WORDS) else PRICE_NORMAL

class AtmoviesCollector(Collector):
    name = "atmovies"
    needs_coverage = True

    def __init__(self, coverage: Coverage, fetcher: Fetcher | None = None, today: dt.date | None = None):
        self.coverage = coverage
        self.fetcher = fetcher or Fetcher()
        self.today = today

    def collect(self, start: dt.date, days: int) -> Batch:
        today = self.today or today_tw()
        last_site_day = today + dt.timedelta(days=SITE_DAYS - 1)
        dates = [d for d in (start + dt.timedelta(days=i) for i in range(days)) if today <= d <= last_site_day]
        regions = sorted({REGION_BY_CITY[c] for c in self.coverage.cities() if c in REGION_BY_CITY})
        if not dates or not regions:
            return Batch()

        self.failed = self.skipped = self.attempted = 0
        cinemas: list[CinemaRecord] = []
        films: dict[str, FilmShowing] = {}  # 電影編號 → 第一次看到的片名、片長
        showtimes: list[ShowtimeRecord] = []
        for region in regions:
            theaters = parse_region(self.fetcher.get(region_url(region)), region)
            if not theaters:
                raise ScrapeError(f"開眼地區頁 {region} 解析不到影城，網站可能改版了")
            for theater in theaters:
                result = self._theater(theater, region, dates, today)
                if result is None:
                    continue
                cinema, theater_films, theater_showtimes = result
                cinemas.append(cinema)
                for film in theater_films:
                    films.setdefault(film.movie_id, film)
                showtimes += theater_showtimes

        if not cinemas and self.attempted:
            raise ScrapeError("開眼：商圈內的影城全部抓取失敗")
        if cinemas and not showtimes:
            raise ScrapeError("開眼：影城頁面都沒有場次，網站可能改版了")

        movies = [self._movie(film) for film in films.values()]
        return Batch(
            cinemas=cinemas,
            movies=movies,
            showtimes=showtimes,
            showtime_dates=(dates[0], dates[-1]),
            showtime_cinemas=[c.external_id for c in cinemas],
            stats={"failed_pages": self.failed, "showtimes_skipped_no_runtime": self.skipped},
        )

    def _theater(
        self, theater: TheaterLink, region: str, dates: list[dt.date], today: dt.date
    ) -> tuple[CinemaRecord, list[FilmShowing], list[ShowtimeRecord]] | None:
        """抓一家影城。不在商圈範圍、或任何一頁抓失敗就回傳 None（保留這家影城上次的場次）。

        self.attempted 記有多少家影城在商圈內（或地圖抓不到、不知道在不在）而試著抓過，
        用來分辨「商圈內的影城全部失敗」和「商圈附近本來就沒有影城」。
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
        area = self.coverage.area_for(*point)
        if area is None:
            return None
        self.attempted += 1

        pages = []
        for day in dates:
            url = theater_url(theater.tid, region, day, today)
            try:
                page = parse_theater_day(self.fetcher.get(url))
            except (FetchError, ValueError):  # 抓不到、或頁面壞掉（例如日期不存在）：只跳過這家
                self.failed += 1
                return None
            if page.date != day:  # 網站正在換日或頁面不對，這家這次先不更新
                self.failed += 1
                return None
            pages.append((day, url, page))

        cinema = CinemaRecord(
            external_id=theater.tid, name=theater.name[:NAME_MAX], area_id=area,
            address=pages[0][2].address[:ADDRESS_MAX],
            lat=point[0], lng=point[1], source_url=theater_url(theater.tid, region, today, today),
        )
        films: list[FilmShowing] = []
        showtimes: dict[str, ShowtimeRecord] = {}
        for day, url, page in pages:
            for film in page.films:
                if film.runtime_min is None:
                    self.skipped += len(film.times)
                    continue
                films.append(film)
                label = (film.version or DEFAULT_LABEL)[:LABEL_MAX]
                for start in film.times:
                    eid = f"{theater.tid}|{film.movie_id}|{label}|{day.isoformat()}|{start}"
                    showtimes[eid] = ShowtimeRecord(
                        external_id=eid, cinema_external_id=theater.tid, movie_external_id=film.movie_id,
                        raw_title=film.title[:TITLE_MAX], date=day, start=start, label=label, price=price_for(label),
                        source_url=url, estimated=["price"],
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
        return MovieRecord(
            external_id=film.movie_id, title=film.title[:TITLE_MAX], runtime_min=film.runtime_min,
            synopsis=synopsis, source_url=url,
        )

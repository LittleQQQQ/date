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

BASE_URL = "https://www.atmovies.com.tw"
MAP_URL = "http://ww2.atmovies.com.tw/movie/GMap2018.cfm?tid={tid}"
SITE_DAYS = 4  # 網站提供今天起幾天的時刻表
CACHE_DAYS = 30  # 地圖頁、電影頁很少變
SYNOPSIS_MAX = 600
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

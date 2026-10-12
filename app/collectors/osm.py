"""OpenStreetMap 餐廳爬蟲（透過 Overpass API）。

OSM 有店名、座標，部分店家有料理類型（cuisine）與營業時間（opening_hours）；沒有評論和價位。
- 介紹由標籤組成，給 Jev 判斷約會適合度、用餐快慢、料理類型。
- 沒有營業時間或看不懂的，依店的類型估計；價位一律依類型估計。估計的欄位記在 estimated，畫面會標示。
資料授權：ODbL，畫面要標示「© OpenStreetMap 貢獻者」。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter

from ..geo import DETOUR, WALK_M_PER_MIN
from ..planner import STATION_WALK_MAX
from ..timeutil import WEEKDAY_KEYS, hhmm
from .base import Batch, Collector, RestaurantRecord, ScrapeError
from .coverage import AreaCoverage, Coverage, Point
from .http import Fetcher, FetchError

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SEARCH_RADIUS_M = round(STATION_WALK_MAX * WALK_M_PER_MIN / DETOUR)  # 步行 15 分鐘的直線距離，約 865 公尺
MAX_RESTAURANTS_PER_AREA = 60  # 太多會讓排程組合暴增，Jev 選餐廳的選項也會太多

AMENITIES = ("restaurant", "cafe", "fast_food", "bar", "pub")
KIND_WORDS = {"restaurant": "餐廳", "cafe": "咖啡廳", "fast_food": "速食店", "bar": "酒館", "pub": "酒館"}
RESERVABLE = ("yes", "recommended", "required")

# OSM 的 cuisine 值 → (介紹裡的中文, 專案的料理類別)
CUISINES: dict[str, tuple[str, str]] = {
    "japanese": ("日式料理", "日式"), "ramen": ("拉麵", "日式"), "sushi": ("壽司", "日式"),
    "udon": ("烏龍麵", "日式"), "izakaya": ("居酒屋", "日式"), "donburi": ("丼飯", "日式"),
    "tonkatsu": ("豬排", "日式"),
    "korean": ("韓式料理", "韓式"),
    "chinese": ("中式料理", "中式／台式"), "taiwanese": ("台式料理", "中式／台式"),
    "dumpling": ("餃子", "中式／台式"), "noodle": ("麵食", "中式／台式"), "dim_sum": ("點心", "中式／台式"),
    "cantonese": ("粵菜", "中式／台式"), "beef_noodle": ("牛肉麵", "中式／台式"),
    "italian": ("義式料理", "義式／西式"), "pizza": ("披薩", "義式／西式"), "pasta": ("義大利麵", "義式／西式"),
    "french": ("法式料理", "義式／西式"), "steak_house": ("排餐", "義式／西式"),
    "western": ("西式料理", "義式／西式"), "european": ("歐式料理", "義式／西式"),
    "spanish": ("西班牙料理", "義式／西式"),
    "american": ("美式料理", "美式"), "burger": ("漢堡", "美式"), "chicken": ("雞肉料理", "美式"),
    "thai": ("泰式料理", "泰式／南洋"), "vietnamese": ("越南料理", "泰式／南洋"),
    "indonesian": ("印尼料理", "泰式／南洋"), "malaysian": ("馬來西亞料理", "泰式／南洋"),
    "singaporean": ("新加坡料理", "泰式／南洋"), "indian": ("印度料理", "泰式／南洋"),
    "hot_pot": ("火鍋", "火鍋／燒肉"), "barbecue": ("燒烤", "火鍋／燒肉"), "bbq": ("燒烤", "火鍋／燒肉"),
    "yakiniku": ("燒肉", "火鍋／燒肉"), "shabu-shabu": ("涮涮鍋", "火鍋／燒肉"),
    "coffee_shop": ("咖啡", "咖啡／甜點"), "dessert": ("甜點", "咖啡／甜點"), "cake": ("蛋糕", "咖啡／甜點"),
    "ice_cream": ("冰品", "咖啡／甜點"), "bubble_tea": ("手搖飲", "咖啡／甜點"),
}

# 沒有營業時間時依類型估計（酒館營業到次日 01:00）
ESTIMATED_HOURS = {
    "restaurant": ("11:30", "21:00"),
    "cafe": ("10:00", "19:00"),
    "fast_food": ("10:00", "22:00"),
    "bar": ("19:00", "25:00"),
    "pub": ("19:00", "25:00"),
}
ESTIMATED_PRICE = {"restaurant": 500, "cafe": 300, "fast_food": 200, "bar": 700, "pub": 700}
PRICEY_CATEGORIES = ("火鍋／燒肉", "義式／西式")

_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_SPAN_TEXT = r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}"
_RULE = re.compile(
    rf"^(?:(?P<days>[A-Za-z][A-Za-z,\- ]*?)\s+)?(?P<times>off|closed|{_SPAN_TEXT}(?:\s*,\s*{_SPAN_TEXT})*)$"
)
_SPAN = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")


def _parse_days(text: str) -> list[int] | None:
    days: list[int] = []
    for part in text.replace(" ", "").split(","):
        if part == "PH":  # 國定假日的規則忽略
            continue
        if "-" in part:
            first, last = part.split("-", 1)
            if first not in _DAYS or last not in _DAYS:
                return None
            i, j = _DAYS.index(first), _DAYS.index(last)
            days += [(i + k) % 7 for k in range((j - i) % 7 + 1)]  # 可以跨週，例如 Su-Th
        elif part in _DAYS:
            days.append(_DAYS.index(part))
        else:
            return None
    return days


def _fmt(span: tuple[int, int] | None) -> list[str] | None:
    return None if span is None else [hhmm(span[0]), hhmm(span[1])]


def parse_opening_hours(text: str) -> dict[str, list[str] | None] | None:
    """把 OSM 的 opening_hours 轉成專案格式；看不懂的寫法回傳 None（改用估計值）。

    同一天分段營業時取最後一段（晚餐時段），寧可少排，也不要排到中間休息的時間。
    國定假日（PH）只當作日期清單裡可忽略的一項；整條規則都只適用於 PH，或根本沒有規則適用於
    星期一到日時，視為看不懂。
    """
    text = (text or "").strip()
    if not text:
        return None
    week: list[tuple[int, int] | None] = [None] * 7  # OSM 沒寫到的星期視為公休
    applied = False  # 有沒有規則適用於星期一到日
    all_day = False  # 有沒有 24/7
    for rule in filter(None, (r.strip() for r in text.split(";"))):
        if rule == "24/7":
            all_day = True
            continue
        m = _RULE.match(rule)
        if not m:
            return None
        days = _parse_days(m.group("days")) if m.group("days") else list(range(7))
        if days is None:
            return None
        if not days:  # 只適用於 PH 的規則
            continue
        if m.group("times") in ("off", "closed"):
            span = None
        else:
            o_h, o_m, c_h, c_m = map(int, _SPAN.findall(m.group("times"))[-1])
            open_, close = o_h * 60 + o_m, c_h * 60 + c_m
            if max(o_m, c_m) > 59 or open_ >= 24 * 60 or close > 24 * 60 or open_ == close:
                return None
            if close < open_:  # 營業到隔天凌晨
                close += 24 * 60
            span = (open_, close)
        applied = True
        for day in days:
            week[day] = span
    if all_day:  # 24/7 和其他星期的規則混在一起的寫法不處理
        return None if applied else {"default": ["00:00", "24:00"]}
    if not applied:
        return None
    counts = Counter(week)
    default = max(counts, key=lambda s: (counts[s], s is not None))  # 一樣多時，營業的時段優先
    hours = {"default": _fmt(default)}
    for i, span in enumerate(week):
        if span != default:
            hours[WEEKDAY_KEYS[i]] = _fmt(span)
    return hours


def _cuisine_tokens(tags: dict) -> list[str]:
    return [t.strip().lower().replace(" ", "_") for t in tags.get("cuisine", "").split(";") if t.strip()]


def cuisine_category(tags: dict) -> str | None:
    amenity = tags.get("amenity")
    if amenity == "cafe":
        return "咖啡／甜點"
    if amenity in ("bar", "pub"):
        return "酒館"
    for token in _cuisine_tokens(tags):
        if token in CUISINES:
            return CUISINES[token][1]
    return None


def describe(tags: dict, category: str | None) -> str:
    amenity = tags.get("amenity", "restaurant")
    kind = KIND_WORDS.get(amenity, "餐廳")
    # 咖啡廳、酒館本身就說明了類型；餐廳、速食店前面加上料理類別
    parts = [kind if amenity in ("cafe", "bar", "pub") or not category else f"{category}{kind}"]
    dishes: list[str] = []
    for token in _cuisine_tokens(tags):
        word = CUISINES.get(token, ("",))[0]
        if word and not word.endswith("料理") and word not in dishes:
            dishes.append(word)
    if dishes:
        parts.append(f"供應{'、'.join(dishes)}")
    if tags.get("diet:vegetarian") in ("yes", "only") or tags.get("diet:vegan") in ("yes", "only"):
        parts.append("有素食選項")
    if tags.get("reservation") in RESERVABLE:
        parts.append("可訂位")
    if tags.get("takeaway") == "only":
        parts.append("以外帶為主")
    if tags.get("outdoor_seating") == "yes":
        parts.append("有戶外座位")
    text = "，".join(parts) + "。"
    if extra := tags.get("description", "").strip():
        text += extra if extra.endswith(("。", ".", "！", "!")) else f"{extra}。"
    return text


def place_hours(tags: dict) -> tuple[dict[str, list[str] | None], bool]:
    """營業時間與「是不是估計的」。"""
    if parsed := parse_opening_hours(tags.get("opening_hours", "")):
        return parsed, False
    open_, close = ESTIMATED_HOURS.get(tags.get("amenity", ""), ESTIMATED_HOURS["restaurant"])
    return {"default": [open_, close]}, True


def estimate_price(amenity: str, category: str | None) -> int:
    if amenity == "restaurant" and category in PRICEY_CATEGORIES:
        return 800
    return ESTIMATED_PRICE.get(amenity, ESTIMATED_PRICE["restaurant"])


def completeness(tags: dict) -> int:
    """資料完整度：有看得懂的營業時間 +2、有對得到的料理類別 +1。挑店時優先留資料完整的。"""
    has_hours = parse_opening_hours(tags.get("opening_hours", "")) is not None
    return 2 * has_hours + (cuisine_category(tags) is not None)


def overpass_query(stations: list[Point]) -> str:
    amenity = "|".join(AMENITIES)
    lines = [
        f'  nwr["amenity"~"^({amenity})$"]["name"](around:{SEARCH_RADIUS_M},{p.lat:.6f},{p.lng:.6f});'
        for p in stations
    ]
    return "[out:json][timeout:60];\n(\n" + "\n".join(lines) + "\n);\nout center tags;"


def _point(element: dict) -> Point | None:
    if "lat" in element and "lon" in element:
        return Point(element["lat"], element["lon"])
    if center := element.get("center"):
        return Point(center["lat"], center["lon"])
    return None


def _address(tags: dict) -> str:
    if full := tags.get("addr:full"):
        return full
    return "".join(tags.get(k, "") for k in ("addr:city", "addr:district", "addr:street", "addr:housenumber"))


def to_record(element: dict, area_id: str) -> RestaurantRecord | None:
    tags = element.get("tags", {})
    point = _point(element)
    if point is None or not tags.get("name"):
        return None
    amenity = tags.get("amenity", "restaurant")
    category = cuisine_category(tags)
    hours, hours_estimated = place_hours(tags)
    osm_id = f"{element['type']}/{element['id']}"
    return RestaurantRecord(
        external_id=osm_id,
        name=tags["name"][:50],
        area_id=area_id,
        address=_address(tags)[:200],
        lat=point.lat,
        lng=point.lng,
        description=describe(tags, category),
        price_per_person=estimate_price(amenity, category),
        hours=hours,
        reservable=tags.get("reservation") in RESERVABLE,
        source_url=f"https://www.openstreetmap.org/{osm_id}",
        estimated=(["hours"] if hours_estimated else []) + ["price"],
    )


class OsmCollector(Collector):
    name = "osm"
    needs_coverage = True

    def __init__(self, coverage: Coverage, fetcher: Fetcher | None = None):
        self.coverage = coverage
        # Overpass 忙的時候回應很慢、常回 504，要等久一點再試（15 秒、30 秒）
        self.fetcher = fetcher or Fetcher(timeout=120, backoff=15)

    def collect(self, start: dt.date, days: int) -> Batch:
        restaurants: list[RestaurantRecord] = []
        done: list[str] = []
        failed = 0
        for area in self.coverage.areas:
            elements = self._query(area)
            if elements is None:
                failed += 1
                continue
            restaurants += self._pick(area, elements)
            done.append(area.id)
        if self.coverage.areas and not done:
            raise ScrapeError("Overpass API 所有商圈都查詢失敗")
        return Batch(restaurants=restaurants, restaurant_areas=done, stats={"failed_areas": failed})

    def _query(self, area: AreaCoverage) -> list[dict] | None:
        """查一個商圈的店家；失敗時回傳 None（連線失敗、不是 JSON、Overpass 回報執行錯誤）。

        Overpass 逾時或記憶體不足時，仍然回 HTTP 200，只在 remark 寫「runtime error」，
        不能當成「這個商圈沒有店」，否則該商圈既有的店會被當成歇業刪掉。
        """
        try:
            data = json.loads(self.fetcher.post(OVERPASS_URL, {"data": overpass_query(area.stations)}))
        except (FetchError, ValueError):
            return None
        if not isinstance(data, dict) or "runtime error" in str(data.get("remark", "")).lower():
            return None
        elements = data.get("elements")  # 沒有 elements（代理伺服器的 JSON 錯誤、只有 remark）不是「沒有店」
        return elements if isinstance(elements, list) else None

    def _pick(self, area: AreaCoverage, elements: list[dict]) -> list[RestaurantRecord]:
        """一個商圈最多留 MAX_RESTAURANTS_PER_AREA 家：資料完整的優先，其次離影城近的。"""
        ranked = []
        seen: set[str] = set()
        for element in elements:
            point = _point(element)
            if point is None or self.coverage.area_for(point.lat, point.lng) != area.id:
                continue  # 離別的商圈更近（或超出範圍），交給那個商圈
            record = to_record(element, area.id)
            if record is None or record.external_id in seen:
                continue
            seen.add(record.external_id)
            tags = element.get("tags", {})
            ranked.append((-completeness(tags), area.walk_to_cinemas(point), record.name, record))
        ranked.sort(key=lambda item: item[:3])
        return [item[3] for item in ranked[:MAX_RESTAURANTS_PER_AREA]]

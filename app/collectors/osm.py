"""OpenStreetMap 餐廳爬蟲（透過 Overpass API，規格書 7.2 refresh_daily 的備案餐廳來源）。

資料庫只存 OSM 的原始資料：店名（osm_name）、營業時間字串（osm_opening_hours）、所有標籤（osm_tags）。
這個檔案的其他函式把標籤轉成排程和 Jev 要用的東西（營業時間、價位估計、介紹、料理類別），排程時才算：
- 介紹由標籤組成，給 Jev 判斷餐廳類別。
- 沒有營業時間或看不懂的，依店的類型估計；價位一律依類型估計（畫面要標示估計）。
餐廳屬於哪個區域、哪個類別由 Jev 判斷；cuisine_category 是根據標籤的猜測，給 Jev 模擬版和排程參考。
資料授權：ODbL，畫面要標示「© OpenStreetMap 貢獻者」。
"""
from __future__ import annotations

import json
import re
from collections import Counter

from sqlalchemy.orm import Session

from ..timeutil import WEEKDAY_KEYS, hhmm
from .base import Batch, Collector, RestaurantRecord, ScrapeError
from .coverage import WALK_RADIUS_M, Coverage, CoveredStation, load_coverage
from .http import Fetcher, FetchError

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SEARCH_RADIUS_M = WALK_RADIUS_M  # 車站 800 公尺內的店（規格書 7.1 用同一個門檻判斷走路）
MAX_RESTAURANTS_PER_AREA = 60  # 太多會讓排程組合暴增

AMENITIES = ("restaurant", "cafe", "fast_food", "bar", "pub")
KIND_WORDS = {"restaurant": "餐廳", "cafe": "咖啡廳", "fast_food": "速食店", "bar": "酒館", "pub": "酒館"}
RESERVABLE = ("yes", "recommended", "required")

# OSM 的 cuisine 值 → (介紹裡的中文, 料理類別代碼（data/seed/categories.json）)
CUISINES: dict[str, tuple[str, str]] = {
    "japanese": ("日式料理", "japanese"), "ramen": ("拉麵", "japanese"), "sushi": ("壽司", "japanese"),
    "udon": ("烏龍麵", "japanese"), "izakaya": ("居酒屋", "japanese"), "donburi": ("丼飯", "japanese"),
    "tonkatsu": ("豬排", "japanese"),
    "korean": ("韓式料理", "korean"),
    "chinese": ("中式料理", "taiwanese"), "taiwanese": ("台式料理", "taiwanese"),
    "dumpling": ("餃子", "taiwanese"), "noodle": ("麵食", "taiwanese"), "dim_sum": ("點心", "taiwanese"),
    "cantonese": ("粵菜", "taiwanese"), "beef_noodle": ("牛肉麵", "taiwanese"),
    "italian": ("義式料理", "western"), "pizza": ("披薩", "western"), "pasta": ("義大利麵", "western"),
    "french": ("法式料理", "western"), "steak_house": ("排餐", "western"),
    "western": ("西式料理", "western"), "european": ("歐式料理", "western"),
    "spanish": ("西班牙料理", "western"),
    "american": ("美式料理", "american"), "burger": ("漢堡", "american"), "chicken": ("雞肉料理", "american"),
    "thai": ("泰式料理", "southeast_asian"), "vietnamese": ("越南料理", "southeast_asian"),
    "indonesian": ("印尼料理", "southeast_asian"), "malaysian": ("馬來西亞料理", "southeast_asian"),
    "singaporean": ("新加坡料理", "southeast_asian"), "indian": ("印度料理", "southeast_asian"),
    "hot_pot": ("火鍋", "hotpot_bbq"), "barbecue": ("燒烤", "hotpot_bbq"), "bbq": ("燒烤", "hotpot_bbq"),
    "yakiniku": ("燒肉", "hotpot_bbq"), "shabu-shabu": ("涮涮鍋", "hotpot_bbq"),
    "coffee_shop": ("咖啡", "cafe_dessert"), "dessert": ("甜點", "cafe_dessert"), "cake": ("蛋糕", "cafe_dessert"),
    "ice_cream": ("冰品", "cafe_dessert"), "bubble_tea": ("手搖飲", "cafe_dessert"),
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
PRICEY_CATEGORIES = ("hotpot_bbq", "western")
# 介紹裡用的類別名稱（和 data/seed/categories.json 的 label 一致）
CATEGORY_LABELS = {
    "japanese": "日式", "korean": "韓式", "taiwanese": "中式／台式", "western": "義式／西式", "american": "美式",
    "southeast_asian": "泰式／南洋", "hotpot_bbq": "火鍋／燒肉", "cafe_dessert": "咖啡／甜點", "bar": "酒館",
}

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
    return week_hours(week)


def week_hours(week: list[tuple[int, int] | None]) -> dict[str, list[str] | None]:
    """星期一到日的營業時段（分鐘數，None 為公休）→ 專案格式：最常見的時段當 default，其他星期另外列。"""
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
        return "cafe_dessert"
    if amenity in ("bar", "pub"):
        return "bar"
    for token in _cuisine_tokens(tags):
        if token in CUISINES:
            return CUISINES[token][1]
    return None


def describe(tags: dict, category: str | None) -> str:
    amenity = tags.get("amenity", "restaurant")
    kind = KIND_WORDS.get(amenity, "餐廳")
    # 咖啡廳、酒館本身就說明了類型；餐廳、速食店前面加上料理類別
    parts = [kind if amenity in ("cafe", "bar", "pub") or not category else f"{CATEGORY_LABELS.get(category, '')}{kind}"]
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


def overpass_query(stations: list[CoveredStation]) -> str:
    amenity = "|".join(AMENITIES)
    lines = [
        f'  nwr["amenity"~"^({amenity})$"]["name"](around:{SEARCH_RADIUS_M},{p.lat:.6f},{p.lng:.6f});'
        for p in stations
    ]
    return "[out:json][timeout:60];\n(\n" + "\n".join(lines) + "\n);\nout center tags;"


def _lat_lng(element: dict) -> tuple[float, float] | None:
    if "lat" in element and "lon" in element:
        return element["lat"], element["lon"]
    if center := element.get("center"):
        return center["lat"], center["lon"]
    return None


def address(tags: dict) -> str:
    """OSM 標籤組成的地址；沒標就是空字串。"""
    if full := tags.get("addr:full"):
        return full
    return "".join(tags.get(k, "") for k in ("addr:city", "addr:district", "addr:street", "addr:housenumber"))


def to_record(element: dict) -> RestaurantRecord | None:
    tags = {k: str(v) for k, v in element.get("tags", {}).items()}
    point = _lat_lng(element)
    if point is None or not tags.get("name"):
        return None
    return RestaurantRecord(
        external_id=f"{element['type']}/{element['id']}", lat=point[0], lng=point[1],
        osm_name=tags["name"], osm_opening_hours=tags.get("opening_hours"), osm_tags=tags,
    )


class OsmCollector(Collector):
    name = "osm"

    def __init__(self, coverage: Coverage, fetcher: Fetcher | None = None):
        self.coverage = coverage
        # Overpass 忙的時候回應很慢、常回 504，要等久一點再試（15 秒、30 秒）
        self.fetcher = fetcher or Fetcher(timeout=120, backoff=15)

    @classmethod
    def create(cls, session: Session) -> OsmCollector:
        return cls(load_coverage(session))

    def collect(self) -> Batch:
        areas = self.coverage.by_area()
        restaurants: list[RestaurantRecord] = []
        failed = 0
        for area, stations in sorted(areas.items()):
            elements = self._query(stations)
            if elements is None:
                failed += 1
                continue
            restaurants += self._pick(area, elements)
        if areas and failed == len(areas):
            raise ScrapeError("Overpass API 所有區域都查詢失敗")
        # 每個區域都查成功，才能把這次沒出現的店當成收了
        return Batch(restaurants=restaurants, restaurants_complete=failed == 0, stats={"failed_areas": failed})

    def _query(self, stations: list[CoveredStation]) -> list[dict] | None:
        """查一個區域的店家；失敗時回傳 None（連線失敗、不是 JSON、Overpass 回報執行錯誤）。

        Overpass 逾時或記憶體不足時，仍然回 HTTP 200，只在 remark 寫「runtime error」，
        不能當成「這個區域沒有店」，否則該區域既有的店會被當成收了。
        """
        try:
            data = json.loads(self.fetcher.post(OVERPASS_URL, {"data": overpass_query(stations)}))
        except (FetchError, ValueError):
            return None
        if not isinstance(data, dict) or "runtime error" in str(data.get("remark", "")).lower():
            return None
        elements = data.get("elements")  # 沒有 elements（代理伺服器的 JSON 錯誤、只有 remark）不是「沒有店」
        return elements if isinstance(elements, list) else None

    def _pick(self, area: str, elements: list[dict]) -> list[RestaurantRecord]:
        """一個區域最多留 MAX_RESTAURANTS_PER_AREA 家：資料完整的優先，其次離車站近的。"""
        ranked = []
        seen: set[str] = set()
        for element in elements:
            point = _lat_lng(element)
            nearest = self.coverage.nearest(*point) if point else None
            if nearest is None or nearest[0].area_code != area:
                continue  # 離別的區域的車站更近（或超出範圍），交給那個區域
            record = to_record(element)
            if record is None or record.external_id in seen:
                continue
            seen.add(record.external_id)
            ranked.append((-completeness(record.osm_tags), nearest[1], record.osm_name, record))
        ranked.sort(key=lambda item: item[:3])
        return [item[3] for item in ranked[:MAX_RESTAURANTS_PER_AREA]]

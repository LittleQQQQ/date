"""OpenStreetMap 餐廳爬蟲（透過 Overpass API）。

OSM 有店名、座標，部分店家有料理類型（cuisine）與營業時間（opening_hours）；沒有評論和價位。
- 介紹由標籤組成，給 Jev 判斷約會適合度、用餐快慢、料理類型。
- 沒有營業時間或看不懂的，依店的類型估計；價位一律依類型估計。估計的欄位記在 estimated，畫面會標示。
資料授權：ODbL，畫面要標示「© OpenStreetMap 貢獻者」。
"""
from __future__ import annotations

import re
from collections import Counter

from ..timeutil import WEEKDAY_KEYS, hhmm

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
    """
    text = (text or "").strip()
    if not text:
        return None
    if text == "24/7":
        return {"default": ["00:00", "24:00"]}
    week: list[tuple[int, int] | None] = [None] * 7  # OSM 沒寫到的星期視為公休
    for rule in filter(None, (r.strip() for r in text.split(";"))):
        if rule.startswith("PH"):
            continue
        m = _RULE.match(rule)
        if not m:
            return None
        days = _parse_days(m.group("days")) if m.group("days") else list(range(7))
        if days is None:
            return None
        if m.group("times") in ("off", "closed"):
            span = None
        else:
            o_h, o_m, c_h, c_m = map(int, _SPAN.findall(m.group("times"))[-1])
            if max(o_h, c_h) > 24 or max(o_m, c_m) > 59:
                return None
            open_, close = o_h * 60 + o_m, c_h * 60 + c_m
            if close <= open_:  # 營業到隔天凌晨
                close += 24 * 60
            span = (open_, close)
        for day in days:
            week[day] = span
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

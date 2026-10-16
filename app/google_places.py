"""Google Places API (New)：有設定 GOOGLE_PLACES_API_KEY 時，排程當下即時查商圈附近的餐廳。

- Google 的使用條款不允許把店名、營業時間、價位等內容預先抓下來或存進資料庫，所以每次排程都重新查；
  只有 place_id（條款允許長期保存）拿來當 Jev 判斷快取的對象（見 app/jev/service.py 的 place_subject）。
- 商圈的每個車站各查一次 Nearby Search，半徑和 OSM 一樣是步行 15 分鐘；一次最多回傳 20 家、沒有下一頁，
  合併後每個商圈最多留 MAX_RESTAURANTS_PER_AREA 家。
- 計費等級由要求的欄位決定：營業時間、價位屬於 Enterprise；GOOGLE_PLACES_ATMOSPHERE=true 時再要評論、
  可訂位、素食、店家簡介，屬於 Enterprise + Atmosphere。
- 每次呼叫都記在 api_usage 表，當天會超過 GOOGLE_PLACES_DAILY_LIMIT 就不呼叫，由呼叫端改用 OSM 資料。
- 畫面上要標示餐廳資料來自 Google Maps。
"""
from __future__ import annotations

import logging

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .collectors.base import RestaurantRecord
from .collectors.coverage import AreaCoverage
from .collectors.osm import (
    CUISINES,
    ESTIMATED_HOURS,
    MAX_RESTAURANTS_PER_AREA,
    SEARCH_RADIUS_M,
    cuisine_category,
    describe,
    estimate_price,
    week_hours,
)
from .config import settings
from .geo import HasLocation
from .models import ApiUsage
from .timeutil import today_tw

log = logging.getLogger(__name__)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
API_NAME = "google_places"  # api_usage 表的 api 欄位
MAX_RESULTS = 20  # Nearby Search 一次最多回傳 20 家
INCLUDED_TYPES = ("restaurant", "cafe", "bar", "pub", "fast_food_restaurant")  # 和 OSM 爬蟲抓的 amenity 對應
FIELDS = (
    "id", "displayName", "formattedAddress", "location", "primaryType", "types", "googleMapsUri", "businessStatus",
    "regularOpeningHours", "priceLevel", "priceRange", "rating", "userRatingCount",
)
ATMOSPHERE_FIELDS = ("reservable", "servesVegetarianFood", "editorialSummary", "reviews")
MAX_REVIEWS = 5
REVIEW_CHARS = 200
CLOSED = ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY")

# 只有價位等級、沒有金額區間時的每人估計
PRICE_LEVELS = {
    "PRICE_LEVEL_INEXPENSIVE": 200,
    "PRICE_LEVEL_MODERATE": 500,
    "PRICE_LEVEL_EXPENSIVE": 1000,
    "PRICE_LEVEL_VERY_EXPENSIVE": 2000,
}
BAR_TYPES = ("bar", "pub", "wine_bar", "cocktail_bar")
CAFE_TYPES = ("cafe", "coffee_shop", "tea_house")
VEGETARIAN_TYPES = ("vegetarian_restaurant", "vegan_restaurant")
# Google 的類型去掉 _restaurant、_shop 後，和 OSM 的 cuisine 值寫法不同的
TYPE_ALIASES = {
    "hamburger": "burger", "korean_barbecue": "bbq", "chinese_noodle": "noodle",
    "shabu_shabu": "shabu-shabu", "bubble_tea_store": "bubble_tea",
}
ERROR_HINTS = {
    400: "金鑰無效或請求格式錯誤",
    401: "金鑰無效",
    403: "金鑰沒有開放 Places API (New)，或帳單帳戶未啟用",
    429: "超過 Google 的配額",
}


class PlacesError(Exception):
    """Google Places 查不到（連線失敗、金鑰或帳單有問題、超過配額或每日上限）；呼叫端改用 OSM 資料。"""


# ---------- Google 的資料 → 專案格式 ----------


def _cuisine_key(place_type: str) -> str | None:
    """Google 的類型 → OSM 的 cuisine 值，例如 ramen_restaurant → ramen；對不到回傳 None。"""
    for key in (place_type, place_type.removesuffix("_restaurant"), place_type.removesuffix("_shop")):
        key = TYPE_ALIASES.get(key, key)
        if key in CUISINES:
            return key
    return None


def _amenity(types: list[str]) -> str:
    """店的類型，用 OSM 的 amenity 表示，沿用 OSM 爬蟲的類型文字與估計值。主要類型排在最前面，先對到的算數。"""
    for t in types:
        if t in BAR_TYPES:
            return "bar"
        if t in CAFE_TYPES:
            return "cafe"
        if t == "fast_food_restaurant":
            return "fast_food"
        if t == "restaurant" or t.endswith("_restaurant"):
            return "restaurant"
    return "restaurant"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text.endswith(("。", ".", "！", "!")) else f"{text}。"


def to_tags(place: dict) -> dict[str, str]:
    """把 Google 的資料轉成 OSM 標籤的樣子，沿用 OSM 爬蟲的料理分類與介紹文字。"""
    types = [t for t in (place.get("primaryType", ""), *place.get("types", [])) if t]
    keys = list(dict.fromkeys(k for t in types if (k := _cuisine_key(t))))
    tags = {"amenity": _amenity(types), "cuisine": ";".join(keys)}
    if place.get("servesVegetarianFood") or any(t in VEGETARIAN_TYPES for t in types):
        tags["diet:vegetarian"] = "yes"
    if place.get("reservable"):
        tags["reservation"] = "yes"
    extra = ""
    if rating := place.get("rating"):
        extra += f"Google 評分 {rating:.1f}（{place.get('userRatingCount', 0)} 則評論）。"
    extra += _sentence((place.get("editorialSummary") or {}).get("text", ""))
    if extra:
        tags["description"] = extra
    return tags


def parse_hours(opening: dict | None) -> dict[str, list[str] | None] | None:
    """Google 的 regularOpeningHours → 專案格式；沒有或看不懂時回傳 None（改用估計值）。

    Google 的星期 0 是星期日。只有一段、從 00:00 開始又沒有結束時間的，是 24 小時營業。
    同一天分段營業時取最後一段（晚餐時段），和 OSM 一樣；沒列到的星期視為公休。
    """
    periods = (opening or {}).get("periods") or []
    if len(periods) == 1 and "close" not in periods[0]:
        start = periods[0].get("open") or {}
        return {"default": ["00:00", "24:00"]} if not start.get("hour") and not start.get("minute") else None
    week: list[tuple[int, int] | None] = [None] * 7
    for period in periods:
        o, c = period.get("open"), period.get("close")
        if not o or not c or "day" not in o or "day" not in c:
            return None
        start = o.get("hour", 0) * 60 + o.get("minute", 0)
        end = (c["day"] - o["day"]) % 7 * 24 * 60 + c.get("hour", 0) * 60 + c.get("minute", 0)
        if end <= start:
            return None
        end = min(end, start + 24 * 60)  # 連續營業超過一天的，當天只算 24 小時
        day = (o["day"] - 1) % 7  # 換成星期一 = 0
        if week[day] is None or start > week[day][0]:
            week[day] = (start, end)
    return week_hours(week) if any(week) else None


def _twd(money: dict | None) -> int | None:
    if not money or money.get("currencyCode", "TWD") != "TWD":
        return None
    try:
        return int(money.get("units", 0)) or None
    except (TypeError, ValueError):
        return None


def place_price(place: dict, amenity: str, category: str | None) -> tuple[int, bool]:
    """每人價位與「是不是估計的」：有金額區間取中間值；只有價位等級或都沒有時用估計值。"""
    price_range = place.get("priceRange") or {}
    values = [v for v in (_twd(price_range.get("startPrice")), _twd(price_range.get("endPrice"))) if v]
    if values:
        return round(sum(values) / len(values) / 10) * 10, False
    if level := PRICE_LEVELS.get(place.get("priceLevel", "")):
        return level, True
    return estimate_price(amenity, category), True


def to_record(place: dict, area_id: str) -> RestaurantRecord | None:
    """一家店 → RestaurantRecord（只在記憶體裡用，不存進資料庫）；缺必要資料或已歇業回傳 None。"""
    name = ((place.get("displayName") or {}).get("text") or "").strip()
    location = place.get("location") or {}
    if not place.get("id") or not name or "latitude" not in location or "longitude" not in location:
        return None
    if place.get("businessStatus") in CLOSED:
        return None
    tags = to_tags(place)
    amenity, category = tags["amenity"], cuisine_category(tags)
    estimated = []
    hours = parse_hours(place.get("regularOpeningHours"))
    if hours is None:
        hours = {"default": list(ESTIMATED_HOURS[amenity])}
        estimated.append("hours")
    price, price_estimated = place_price(place, amenity, category)
    if price_estimated:
        estimated.append("price")
    reviews = [
        text[:REVIEW_CHARS]
        for review in (place.get("reviews") or [])[:MAX_REVIEWS]
        if (text := ((review.get("text") or {}).get("text") or "").strip())
    ]
    try:
        return RestaurantRecord(
            external_id=place["id"],
            name=name,
            area_id=area_id,
            address=place.get("formattedAddress", ""),
            lat=location["latitude"],
            lng=location["longitude"],
            description=describe(tags, category),
            reviews=reviews,
            price_per_person=price,
            hours=hours,
            reservable=bool(place.get("reservable")),
            source_url=place.get("googleMapsUri", ""),
            estimated=estimated,
        )
    except ValidationError:
        log.warning("Google 的店家資料格式不對，略過：%s", place["id"])
        return None


# ---------- 呼叫 API ----------


def _error_text(resp: httpx.Response) -> str:
    try:
        error = resp.json().get("error", {})
    except (ValueError, AttributeError):
        error = {}
    log.warning("Google Places 回應 %s：%s", resp.status_code, str(error.get("message", ""))[:300])
    hint = ERROR_HINTS.get(resp.status_code) or ("Google 伺服器錯誤" if resp.status_code >= 500 else "查詢失敗")
    status = f" {error['status']}" if error.get("status") else ""
    return f"{hint}，HTTP {resp.status_code}{status}"


class PlacesClient:
    def __init__(self, api_key: str, atmosphere: bool = True, client: httpx.Client | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.field_mask = ",".join(f"places.{f}" for f in FIELDS + (ATMOSPHERE_FIELDS if atmosphere else ()))
        self.client = client or httpx.Client(timeout=timeout)

    def __enter__(self) -> PlacesClient:
        return self

    def __exit__(self, *exc) -> None:
        self.client.close()

    def search_nearby(self, center: HasLocation, radius_m: int) -> list[dict]:
        """center 周圍 radius_m 公尺內的餐廳，最多 20 家（依熱門程度排序）。"""
        body = {
            "includedTypes": list(INCLUDED_TYPES),
            "maxResultCount": MAX_RESULTS,
            "locationRestriction": {
                "circle": {"center": {"latitude": center.lat, "longitude": center.lng}, "radius": float(radius_m)}
            },
            "rankPreference": "POPULARITY",
            "languageCode": "zh-TW",
            "regionCode": "TW",
        }
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": self.field_mask}
        try:
            resp = self.client.post(SEARCH_URL, json=body, headers=headers)
        except httpx.RequestError as exc:
            raise PlacesError(f"連不上 Google（{type(exc).__name__}）") from exc
        if resp.status_code != 200:
            raise PlacesError(_error_text(resp))
        try:
            data = resp.json()
        except ValueError as exc:
            raise PlacesError("Google 回傳的不是 JSON") from exc
        places = data.get("places", []) if isinstance(data, dict) else None  # 附近沒有店時回傳 {}
        if not isinstance(places, list):
            raise PlacesError("Google 回傳的格式不對")
        return places


def make_client() -> PlacesClient:
    return PlacesClient(settings.google_places_api_key.strip(), settings.google_places_atmosphere)


def use_quota(session: Session, calls: int) -> bool:
    """今天還能再呼叫 calls 次就記下來、回傳 True；會超過 GOOGLE_PLACES_DAILY_LIMIT 就一次都不記、回傳 False。

    會 commit，所以要在 session 還沒有其他待寫入的資料時呼叫。
    """
    day = today_tw().isoformat()
    for _ in range(2):  # 兩個請求同時寫入當天第一筆時，後寫的會衝突，重讀一次
        row = session.scalar(select(ApiUsage).where(ApiUsage.api == API_NAME, ApiUsage.day == day))
        used = row.calls if row else 0
        if used + calls > settings.google_places_daily_limit:
            return False
        if row is None:
            row = ApiUsage(api=API_NAME, day=day, calls=0)
            session.add(row)
        row.calls = used + calls
        try:
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
    return False


def _search(session: Session, client: PlacesClient, circles: list[tuple[HasLocation, int]]) -> list[dict]:
    """查每個圓圈範圍，合併去重。次數不夠查完全部圓圈時一次都不查，免得只查到一部分的車站。"""
    if not use_quota(session, len(circles)):
        raise PlacesError("今天的查詢次數已達上限")
    found: dict[str, dict] = {}
    for center, radius in circles:
        for place in client.search_nearby(center, radius):
            if isinstance(place, dict) and place.get("id"):
                found.setdefault(place["id"], place)
    return list(found.values())


def area_restaurants(session: Session, area: AreaCoverage, client: PlacesClient) -> list[RestaurantRecord]:
    """商圈每個車站各查一次，最多留 MAX_RESTAURANTS_PER_AREA 家：有營業時間的優先，其次離影城近的。"""
    ranked = []
    for place in _search(session, client, [(station, SEARCH_RADIUS_M) for station in area.stations]):
        if record := to_record(place, area.id):
            ranked.append(("hours" in record.estimated, area.walk_to_cinemas(record), record.name, record))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:MAX_RESTAURANTS_PER_AREA]]


def nearby_restaurants(
    session: Session, center: HasLocation, radius_m: int, area_id: str, client: PlacesClient
) -> list[RestaurantRecord]:
    """某個地點（例如影城）周圍的餐廳，查一次。"""
    return [record for place in _search(session, client, [(center, radius_m)]) if (record := to_record(place, area_id))]

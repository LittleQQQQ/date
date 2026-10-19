"""Google Places API (New)（規格書 3.3）。沒有填 GOOGLE_PLACES_API_KEY 時完全不呼叫，餐廳只用 OSM。

| 用途 | API | 欄位 | 計費等級 | 自家每月上限 |
| 寫入時找餐廳（每週） | Nearby Search | id、displayName、types、primaryType、formattedAddress、location | Pro | 4,750 |
| 規劃時補店家資料 | Place Details | displayName、formattedAddress、regularOpeningHours、priceLevel | Enterprise | 950 |
| 每 25 天更新座標 | Place Details | location | Essentials | 9,500 |

條款：只能長期保存 place_id；座標最多存 30 天；店名、地址、營業時間等其他內容不能存進 PostgreSQL 或 Redis。
所以這裡拿到的店家內容只在記憶體裡用（Jev 判斷類別、規劃時排時間軸、推給使用者看），資料庫只存 place_id 和座標。
畫面顯示 Google 的資料時要附 Google 標誌（前端負責）。
"""
from __future__ import annotations

import logging

import httpx
import redis

from ..config import settings
from ..redis_utils import get_redis
from ..redis_utils.quota import MonthlyQuota
from .osm import CUISINES, ESTIMATED_HOURS, week_hours

log = logging.getLogger(__name__)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
NEARBY_FIELDS = ("places.id", "places.displayName", "places.types", "places.primaryType", "places.formattedAddress",
                 "places.location")  # Pro
DETAILS_FIELDS = ("displayName", "formattedAddress", "regularOpeningHours", "priceLevel")  # Enterprise
LOCATION_FIELDS = ("location",)  # Essentials
MAX_RESULTS = 20  # Nearby Search 一次最多 20 家
INCLUDED_TYPES = ("restaurant", "cafe", "bar", "pub", "fast_food_restaurant")

# 只有價位等級時的每人估計（Google 沒給金額）
PRICE_LEVELS = {
    "PRICE_LEVEL_INEXPENSIVE": ("INEXPENSIVE", 250),
    "PRICE_LEVEL_MODERATE": ("MODERATE", 500),
    "PRICE_LEVEL_EXPENSIVE": ("EXPENSIVE", 1000),
    "PRICE_LEVEL_VERY_EXPENSIVE": ("VERY_EXPENSIVE", 2000),
}
BAR_TYPES = ("bar", "pub", "wine_bar", "cocktail_bar")
CAFE_TYPES = ("cafe", "coffee_shop", "tea_house")
TYPE_ALIASES = {"hamburger": "burger", "korean_barbecue": "bbq", "chinese_noodle": "noodle", "shabu_shabu": "shabu-shabu"}
ERROR_HINTS = {400: "金鑰無效或請求格式錯誤", 401: "金鑰無效", 403: "金鑰沒有開放 Places API (New)，或帳單帳戶未啟用", 429: "超過 Google 的配額"}


class PlacesError(Exception):
    """Google 查不到（連線失敗、金鑰或帳單有問題、超過配額）；呼叫端改用 OSM 資料。"""


class QuotaExhausted(PlacesError):
    """自家的每月上限用完了（規格書 5.5）。"""


class PlaceNotFound(PlacesError):
    """place_id 已經不存在（店收了或 Google 換了 ID）。"""


# ---------- Google 的資料 → 專案格式（只在記憶體裡用） ----------


def to_tags(place: dict) -> dict[str, str]:
    """把 Google 的類型轉成 OSM 標籤的樣子，沿用 OSM 那邊的介紹文字、料理類別、價位估計。"""
    types = [t for t in (place.get("primaryType", ""), *place.get("types", [])) if t]
    keys = []
    for t in types:
        for key in (t, t.removesuffix("_restaurant"), t.removesuffix("_shop")):
            key = TYPE_ALIASES.get(key, key)
            if key in CUISINES and key not in keys:
                keys.append(key)
    amenity = "restaurant"
    for t in types:
        if t in BAR_TYPES:
            amenity = "bar"
            break
        if t in CAFE_TYPES:
            amenity = "cafe"
            break
        if t == "fast_food_restaurant":
            amenity = "fast_food"
            break
    return {"amenity": amenity, "cuisine": ";".join(keys), "name": name_of(place)}


def name_of(place: dict) -> str:
    return ((place.get("displayName") or {}).get("text") or "").strip()


def parse_hours(opening: dict | None) -> dict[str, list[str] | None] | None:
    """regularOpeningHours → {"default": [開, 關], "sat": …}；沒有或看不懂時回傳 None（改用估計值）。

    Google 的星期 0 是星期日。只有一段、從 00:00 開始又沒有結束時間的，是 24 小時營業。
    同一天分段營業時取最後一段（晚餐時段）；沒列到的星期視為公休。
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
        end = min(end, start + 24 * 60)
        day = (o["day"] - 1) % 7  # 換成星期一 = 0
        if week[day] is None or start > week[day][0]:
            week[day] = (start, end)
    return week_hours(week) if any(week) else None


def price_of(place: dict) -> tuple[str, int, bool]:
    """（價位等級, 每人估計, 是不是用類型估的）。"""
    if found := PRICE_LEVELS.get(place.get("priceLevel", "")):
        return found[0], found[1], False
    return "MODERATE", 500, True


ESTIMATED_SPAN = ESTIMATED_HOURS["restaurant"]


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


class GooglePlaces:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None, redis_client: redis.Redis | None = None):
        self.api_key = settings.google_places_api_key if api_key is None else api_key
        self.http = client or httpx.Client(timeout=10.0)
        r = redis_client or get_redis()
        self.nearby_quota = MonthlyQuota("google:nearby_pro", settings.google_nearby_monthly_limit, r)
        self.details_quota = MonthlyQuota("google:details_enterprise", settings.google_details_monthly_limit, r)
        self.location_quota = MonthlyQuota("google:details_essentials", settings.google_location_monthly_limit, r)

    def _call(self, quota: MonthlyQuota, method: str, url: str, fields: tuple[str, ...], **kwargs) -> dict:
        if not self.api_key.strip():
            raise PlacesError("沒有設定 GOOGLE_PLACES_API_KEY")
        if not quota.available():
            raise QuotaExhausted(f"{quota.name} 這個月的上限（{quota.limit} 次）用完了")
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": ",".join(fields)}
        try:
            resp = self.http.request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise PlacesError(f"連不上 Google（{type(exc).__name__}）") from exc
        if resp.status_code == 404:
            quota.consume()
            raise PlaceNotFound("Google 找不到這家店")
        if resp.status_code != 200:
            raise PlacesError(_error_text(resp))
        quota.consume()
        try:
            data = resp.json()
        except ValueError as exc:
            raise PlacesError("Google 回傳的不是 JSON") from exc
        if not isinstance(data, dict):
            raise PlacesError("Google 回傳的格式不對")
        return data

    def search_nearby(self, lat: float, lng: float, radius_m: float) -> list[dict]:
        """中心點周圍 radius_m 公尺內的餐廳，最多 20 家（依熱門程度）。計入 Pro 的每月上限。"""
        body = {
            "includedTypes": list(INCLUDED_TYPES), "maxResultCount": MAX_RESULTS, "rankPreference": "POPULARITY",
            "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(radius_m)}},
            "languageCode": "zh-TW", "regionCode": "TW",
        }
        places = self._call(self.nearby_quota, "POST", SEARCH_URL, NEARBY_FIELDS, json=body).get("places", [])
        return [p for p in places if isinstance(p, dict) and p.get("id") and "location" in p]

    def details(self, place_id: str) -> dict:
        """規劃時補店名、地址、營業時間、價位（Enterprise）。只在記憶體裡用。"""
        return self._call(self.details_quota, "GET", DETAILS_URL.format(place_id=place_id), DETAILS_FIELDS,
                          params={"languageCode": "zh-TW", "regionCode": "TW"})

    def location(self, place_id: str) -> tuple[float, float]:
        """更新座標（Essentials）。店不在了丟 PlaceNotFound。"""
        loc = self._call(self.location_quota, "GET", DETAILS_URL.format(place_id=place_id), LOCATION_FIELDS).get("location") or {}
        if "latitude" not in loc or "longitude" not in loc:
            raise PlacesError("Google 沒有回傳座標")
        return loc["latitude"], loc["longitude"]


def use_google() -> bool:
    return bool(settings.google_places_api_key.strip())


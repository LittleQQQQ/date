"""設定：從環境變數或專案根目錄的 .env 讀取（規格書 8.2）。

v2 改寫中：每完成一步，再把那一步用到的設定加進來。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent

# compose.dev.yml 開發環境的資料庫與 Redis；正式環境一定要用 .env 蓋掉
DEV_DATABASE_URL = "postgresql+psycopg://date_planner:dev_only@localhost:5433/date_v2"
DEV_REDIS_URL = "redis://localhost:6380/0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # 只用 PostgreSQL ＋ PostGIS（規格書 4.1）。驅動程式 psycopg 3，同一個網址給同步與非同步共用
    database_url: str = DEV_DATABASE_URL
    # Redis（規格書第 5 節）：佇列、鎖、限流、每月額度、快取、進度推播
    redis_url: str = DEV_REDIS_URL

    # Jev（規格書 3.1）：mock = 規則模擬（沒有金鑰時用），api = TypeSafe 正式 API
    jev_mode: Literal["mock", "api"] = "mock"
    typesafe_api_key: str = ""
    jev_model: str = "jev-latest"
    jev_timeout: float = 10.0
    # 信心值低於這個值就交給 LLM 備援
    jev_confidence_threshold: float = 0.6
    jev_rate_limit_per_minute: int = 300  # 自家上限（官方上限的 1/4），碰到就排隊等
    jev_monthly_budget_micro_usd: int = 1_000_000  # 每月 1 美元，用完全部交給 LLM 備援

    # Ollama（Jev 沒把握時的備援）。留空 = 不用 LLM
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    # 3B 模型在 CPU 上第一次載入要二、三十秒，太短會一開始就逾時，整批都改不了
    ollama_timeout: float = 60.0

    # Google Places API (New)（規格書 3.3）。留空 = 不用 Google，餐廳只用 OSM
    google_places_api_key: str = ""
    # 自家的每月上限：各 SKU 的免費額度留 5% 邊際（規格書 5.5）
    google_nearby_monthly_limit: int = 4750  # Nearby Search Pro（每週找餐廳）
    google_details_monthly_limit: int = 950  # Place Details Enterprise（規劃時補店家資料）；用完就改用 OSM
    google_location_monthly_limit: int = 9500  # Place Details Essentials（每 25 天更新座標）

    # API（規格書 6.1、5.5）。CORS 只允許前端網域，多個用逗號分開；留空 = 不開 CORS（前端和 API 同一個網域）
    cors_origins: str = ""
    api_read_limit_per_minute: int = 60  # 每個 IP 每 60 秒的查詢次數
    api_plan_limit_per_hour: int = 10  # 每個 IP 每小時送出規劃的次數

    # 交通部 TDX（規格書 v0.2 3.2）：每週更新台鐵、公車的班表與票價；留空就用路網骨架的估計值
    tdx_client_id: str = ""
    tdx_client_secret: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def use_tdx(self) -> bool:
        return bool(self.tdx_client_id.strip() and self.tdx_client_secret.strip())


settings = Settings()

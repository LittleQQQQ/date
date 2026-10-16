"""設定：從環境變數或專案根目錄的 .env 讀取。"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .regions import CITY_NAMES

ROOT_DIR = Path(__file__).resolve().parent.parent
SQLITE_FALLBACK = f"sqlite:///{(ROOT_DIR / 'dev.db').as_posix()}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # 留空就用專案資料夾裡的 SQLite（dev.db），方便還沒設定 PostgreSQL 時先跑起來
    database_url: str = ""

    # Jev：mock = 規則模擬（Demo 用）；api = 呼叫正式的 Jev API
    jev_mode: Literal["mock", "api"] = "mock"
    jev_api_url: str = ""
    jev_api_key: str = ""
    jev_timeout: float = 10.0
    # Jev 最高機率低於這個值，就請使用者確認
    jev_confidence_threshold: float = 0.6

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    ollama_timeout: float = 60.0

    # 網頁排程透過 MCP 呼叫排程工具。留空 = 同一個程序內直接連線；
    # 填網址 = 走 HTTP，例如 http://127.0.0.1:8000/mcp
    mcp_client_url: str = ""

    # 爬蟲自動更新：伺服器啟動時補抓，之後每天 refresh_hour 點再抓（測試時關掉）
    auto_refresh: bool = True
    refresh_hour: int = Field(6, ge=0, le=23)

    # 餐廳：填了 Google Places API (New) 的金鑰，排程時就即時查 Google（依條款不存店家資料）；
    # 留空、查詢失敗或超過每日上限時，用資料庫裡每週從 OSM 抓的餐廳
    google_places_api_key: str = ""
    # true = 多要評論、可訂位、素食、店家簡介（計費等級 Enterprise + Atmosphere）；false = 只要營業時間、價位等（Enterprise）
    google_places_atmosphere: bool = True
    # 程式每天最多呼叫 Google Places 幾次（台灣時間換日），和 Cloud Console 的配額雙重把關
    google_places_daily_limit: int = Field(30, ge=0)

    # 交通：填了交通部 TDX 的 Client Id 與 Client Secret，每週從 TDX 更新台鐵、公車的班次與票價；
    # 留空就用 data/fixture/ 手寫的估計值
    tdx_client_id: str = ""
    tdx_client_secret: str = ""

    # 開放哪些縣市（逗號分隔，例如 桃園市,台北市）；留空 = 全台。
    # 範圍外的縣市不列在選單、不能排程、爬蟲不更新，但資料留在資料庫，改回來就能恢復
    active_cities: str = "桃園市"

    @field_validator("active_cities")
    @classmethod
    def _check_active_cities(cls, value: str) -> str:
        unknown = [c.strip() for c in value.split(",") if c.strip() and c.strip() not in CITY_NAMES]
        if unknown:
            raise ValueError(f"ACTIVE_CITIES 有不認得的縣市：{'、'.join(unknown)}（請用「台」字，例如桃園市）")
        return value

    @property
    def db_url(self) -> str:
        return self.database_url.strip() or SQLITE_FALLBACK

    @property
    def use_google_places(self) -> bool:
        return bool(self.google_places_api_key.strip())

    @property
    def use_tdx(self) -> bool:
        return bool(self.tdx_client_id.strip() and self.tdx_client_secret.strip())


settings = Settings()

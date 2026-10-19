"""設定：從環境變數或專案根目錄的 .env 讀取（規格書 8.2）。

v2 改寫中：每完成一步，再把那一步用到的設定加進來。
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent

# compose.dev.yml 開發環境的資料庫；正式環境一定要用 .env 的 DATABASE_URL 蓋掉
DEV_DATABASE_URL = "postgresql+psycopg://date_planner:dev_only@localhost:5433/date_v2"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # 只用 PostgreSQL ＋ PostGIS（規格書 4.1）。驅動程式 psycopg 3，同一個網址給同步與非同步共用
    database_url: str = DEV_DATABASE_URL

    # 交通部 TDX（規格書 v0.2 3.2）：每週更新台鐵、公車的班表與票價；留空就用路網骨架的估計值
    tdx_client_id: str = ""
    tdx_client_secret: str = ""

    @property
    def use_tdx(self) -> bool:
        return bool(self.tdx_client_id.strip() and self.tdx_client_secret.strip())


settings = Settings()

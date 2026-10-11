"""設定：從環境變數或專案根目錄的 .env 讀取。"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

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

    @property
    def db_url(self) -> str:
        return self.database_url.strip() or SQLITE_FALLBACK


settings = Settings()

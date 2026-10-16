import datetime as dt
import os
import tempfile
from pathlib import Path

import pytest

# 必須在 import app 之前設定：測試用獨立的 SQLite、Jev 模擬版、不連 LLM
_TMP = Path(tempfile.mkdtemp(prefix="date-planner-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["JEV_MODE"] = "mock"
os.environ["OLLAMA_URL"] = ""
os.environ["MCP_CLIENT_URL"] = ""
os.environ["AUTO_REFRESH"] = "false"
os.environ["ACTIVE_CITIES"] = ""
os.environ["GOOGLE_PLACES_API_KEY"] = ""  # .env 裡填了金鑰也不要連 Google；要測的測試自己設定
os.environ["TDX_CLIENT_ID"] = ""  # TDX 也一樣：測試不連網，也不耗用金鑰額度
os.environ["TDX_CLIENT_SECRET"] = ""


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.collectors import get_collector
    from app.db import SessionLocal
    from app.ingest import run_collector
    from app.main import app

    with TestClient(app) as c:  # 啟動時只匯入商圈與交通（base）
        with SessionLocal() as s:
            run_collector(s, get_collector("fixture"))  # 測試用的虛構影城、餐廳、場次
        yield c


@pytest.fixture
def session(client):
    from app.db import SessionLocal

    with SessionLocal() as s:
        yield s


@pytest.fixture(scope="session")
def saturday():
    from app.timeutil import today_tw

    today = today_tw()
    return today + dt.timedelta(days=(5 - today.weekday()) % 7)


@pytest.fixture
def mem_session():
    """獨立的記憶體資料庫，給匯入相關的測試用，不影響其他測試共用的示意資料。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app import models  # noqa: F401  註冊資料表
    from app.db import Base

    from sqlalchemy import event

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # 讓 SQLite 也檢查外鍵（和 PostgreSQL 一致），刪除順序錯了測試就會失敗
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s
    engine.dispose()

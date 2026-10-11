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


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:  # 啟動時會自動匯入示意資料
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

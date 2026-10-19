"""測試設定（規格書 8.4）。

- 資料庫用 compose.dev.yml 的 date_v2_test（PostGIS）。每次執行 pytest 先清空，再用 Alembic 建立資料表，
  順便驗證 migration 可以整個退回再重建。
- 每個測試在一個交易裡跑，結束時 rollback，互不影響。
- 不需要資料庫的測試（例如 test_transit.py）沒開 Docker 也能跑。
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://date_planner:dev_only@localhost:5433/date_v2_test"
)
# 必須在 import app 之前設定：app.db 一載入就會用這個網址建立連線
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# .env 裡填了外部服務的金鑰也不要用：測試不連網，也不耗用額度。要測的測試自己設定
os.environ["TDX_CLIENT_ID"] = ""
os.environ["TDX_CLIENT_SECRET"] = ""


@pytest.fixture(scope="session")
def database():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    except OperationalError as exc:
        pytest.exit(
            f"連不上測試資料庫（{exc.orig}）。請先執行：docker compose -f compose.dev.yml up -d --wait", returncode=2
        )
    cfg = Config(ROOT / "alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")  # 退回也要能跑
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def session(database):
    from sqlalchemy.orm import Session

    conn = database.connect()
    trans = conn.begin()
    s = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    yield s
    s.close()
    trans.rollback()
    conn.close()

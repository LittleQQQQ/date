"""Alembic 的執行環境：資料庫網址讀 app/config.py（.env 的 DATABASE_URL），資料表定義讀 app/models.py。

產生新的 migration：  alembic revision --autogenerate -m "說明"
套用到最新版：        alembic upgrade head

alembic.ini 只能有英數字：Windows 預設用 cp950 讀它，寫中文註解會讀不進來。設定的說明寫在這裡。
- file_template：migration 檔名是「日期＋版本＋說明」。
- sqlalchemy.url 留空：網址讀 .env 的 DATABASE_URL（見 _url）。
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app import models  # noqa: F401  註冊所有資料表
from app.config import settings
from app.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """只管程式定義的資料表；PostGIS 自帶的（spatial_ref_sys、tiger、topology 等）不動。"""
    if type_ == "table" and reflected and compare_to is None:
        return False
    return True


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or settings.db_url


def run_migrations_offline() -> None:
    """不連資料庫，只輸出 SQL（alembic upgrade head --sql）。"""
    context.configure(
        url=_url(), target_metadata=target_metadata, literal_binds=True,
        dialect_opts={"paramstyle": "named"}, include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""FastAPI 進入點。v2 改寫中：目前只有健康檢查，/api 的其他端點、/mcp、網頁會在第 7～9 步加回來。"""
from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text

from .db import engine

app = FastAPI(title="約會行程規劃")


@app.get("/api/health")
def health() -> dict:
    """健康檢查：資料庫連得上，而且 PostGIS 有開。"""
    with engine.connect() as conn:
        postgis = conn.execute(text("SELECT postgis_lib_version()")).scalar()
    return {"status": "ok", "database": "ok", "postgis": postgis}

"""程式進入點：uvicorn app.main:app

- /          網頁（frontend/）
- /api/...   REST API
- /mcp       MCP 端點（Streamable HTTP）
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401  註冊資料表
from .api import router
from .config import ROOT_DIR, settings
from .db import Base, SessionLocal, engine, schema_message, schema_problems
from .ingest import ensure_base_data
from .mcp_server import mcp
from .refresher import refresher

mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    if problems := schema_problems():
        raise RuntimeError(schema_message(problems))
    with SessionLocal() as session:
        ensure_base_data(session)
    if settings.auto_refresh:
        refresher.start()  # 背景補抓影城場次與餐廳
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        refresher.stop()


app = FastAPI(title="約會一條龍", lifespan=lifespan)
app.include_router(router, prefix="/api")
app.router.routes.extend(mcp_app.routes)  # 把 /mcp 掛在同一個服務上
app.mount("/", StaticFiles(directory=ROOT_DIR / "frontend", html=True), name="frontend")

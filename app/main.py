"""FastAPI 進入點：/api（規格書第 6 節）和 /mcp（6.7，Streamable HTTP）。網頁在第 9 步加回來。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import errors
from .api.routes import router
from .config import settings
from .mcp_server import MCPEndpoint, new_session_manager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with new_session_manager().run():
        yield


app = FastAPI(title="約會行程規劃", version="2.0", lifespan=lifespan)
errors.install(app)
if settings.cors_origin_list:  # 只允許前端網域（規格書 6.1）；同一個網域就不用開
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"])
app.include_router(router)
app.add_route("/mcp", MCPEndpoint(), methods=["GET", "POST", "DELETE"], include_in_schema=False)

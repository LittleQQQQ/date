"""錯誤格式（規格書 6.1）：一律回 {"error": {"code": "RATE_LIMITED", "message": "請稍後再試"}}。

| HTTP | code | 說明 |
| 400 | VALIDATION_ERROR | 輸入不合法 |
| 404 | NOT_FOUND | 區域或地標不存在 |
| 409 | PLAN_IN_PROGRESS | 這個 IP 已經有一個規劃在進行 |
| 429 | RATE_LIMITED | 超過限流，附 Retry-After |
| 503 | SERVICE_UNAVAILABLE | Redis 或資料庫無法使用 |
"""
from __future__ import annotations

import logging

import redis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..plan_stream import PlanRejected
from ..services import PlanError

log = logging.getLogger(__name__)

STATUS_OF = {
    "VALIDATION_ERROR": 400, "NOT_FOUND": 404, "PLAN_IN_PROGRESS": 409, "RATE_LIMITED": 429, "SERVICE_UNAVAILABLE": 503,
}
HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}

# pydantic 的錯誤類型 → 中文說明（其他類型照 pydantic 的原文）
MESSAGES = {
    "missing": "必填",
    "int_parsing": "要是整數",
    "int_type": "要是整數",
    "int_from_float": "要是整數",
    "string_type": "要是文字",
    "greater_than": "要大於 {gt}",
    "greater_than_equal": "不能小於 {ge}",
    "less_than_equal": "不能大於 {le}",
    "literal_error": "只能是 {expected}",
    "datetime_parsing": "要是 ISO 8601 時間，例如 2026-10-20T17:30:00+08:00",
    "datetime_from_date_parsing": "要是 ISO 8601 時間，例如 2026-10-20T17:30:00+08:00",
    "datetime_type": "要是 ISO 8601 時間，例如 2026-10-20T17:30:00+08:00",
    "json_invalid": "不是合法的 JSON",
    "model_attributes_type": "要是 JSON 物件",
}


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers


def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def validation_message(errors: list[dict]) -> str:
    parts = []
    for err in errors:
        field = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
        template = MESSAGES.get(err.get("type", ""))
        if template:
            text = template.format(**(err.get("ctx") or {}))
        else:
            text = str(err.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{field}：{text}" if field else text)
    return "；".join(parts) or "輸入不合法"


def install(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(400, "VALIDATION_ERROR", validation_message(list(exc.errors())))

    @app.exception_handler(PlanError)
    async def _plan_error(request: Request, exc: PlanError) -> JSONResponse:
        return error_response(STATUS_OF.get(exc.code, 400), exc.code, exc.message)

    @app.exception_handler(PlanRejected)
    async def _rejected(request: Request, exc: PlanRejected) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after_s)} if exc.retry_after_s else None
        return error_response(STATUS_OF[exc.code], exc.code, exc.message, headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
        message = "找不到這個網址" if exc.status_code == 404 else str(exc.detail)
        return error_response(exc.status_code, code, message, getattr(exc, "headers", None))

    @app.exception_handler(redis.exceptions.RedisError)
    async def _redis_down(request: Request, exc: redis.exceptions.RedisError) -> JSONResponse:
        log.error("Redis 無法使用：%s", exc)
        return error_response(503, "SERVICE_UNAVAILABLE", "服務暫時無法使用（Redis），請稍後再試")

    @app.exception_handler(OperationalError)
    async def _db_down(request: Request, exc: OperationalError) -> JSONResponse:
        log.error("資料庫無法使用：%s", exc.orig)
        return error_response(503, "SERVICE_UNAVAILABLE", "服務暫時無法使用（資料庫），請稍後再試")

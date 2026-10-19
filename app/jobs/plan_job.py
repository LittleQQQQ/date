"""規劃工作（規格書 5.6、7.1）：在 RQ worker 的 plans 佇列執行，進度和結果經 Redis Pub/Sub 推回 API。

結果只推送一次、不存（規格書 6.5）；使用者斷線時 API 會取消這個工作（第 7 步）。
"""
from __future__ import annotations

import logging

from pydantic import ValidationError
from rq import get_current_job

from ..db import SessionLocal
from ..planner import plan
from ..progress import Publisher
from ..schemas import PlanRequest
from ..services import PlanError, load_plan_context

log = logging.getLogger(__name__)


def run_plan(request: dict, job_id: str | None = None) -> dict | None:
    """排出方案並推送。回傳值只給測試和 MCP 用（MCP 走同一個佇列，拿 RQ 的結果）。"""
    job_id = job_id or get_current_job().id
    pub = Publisher(job_id)
    try:
        req = PlanRequest.model_validate(request)
        pub.progress("loading_data")
        with SessionLocal() as session:
            ctx = load_plan_context(session, req)
        if not ctx.showtimes:
            raise PlanError("NO_SHOWTIMES", "出發後 12 小時內沒有符合條件的場次")
        result = plan(ctx, progress=pub.progress)
        if not result["plans"]:
            reasons = "、".join(f"{k}（{v}）" for k, v in list(result["rejected"].items())[:3])
            raise PlanError("NO_CANDIDATES", f"找不到接得上的行程。主要卡在：{reasons or '沒有符合條件的餐廳'}")
    except PlanError as exc:
        pub.error(exc.code, exc.message)
        return {"error": {"code": exc.code, "message": exc.message}}
    except ValidationError as exc:
        pub.error("VALIDATION_ERROR", "；".join(e["msg"] for e in exc.errors()))
        return {"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}
    except Exception as exc:  # 外部服務或程式出錯：通知前端後讓 RQ 記成失敗
        log.exception("規劃失敗")
        pub.error("UPSTREAM_ERROR", f"規劃時發生錯誤：{type(exc).__name__}")
        raise
    result = {"job_id": job_id, **result}
    pub.result(result)
    return result

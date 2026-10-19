"""規劃工作（規格書 5.6、7.1）：在 RQ worker 的 plans 佇列執行，進度和結果經 Redis Pub/Sub 推回 API。

結果只推送一次、不存（規格書 6.5）：排進佇列時 result_ttl=0，RQ 也不保存回傳值。
使用者斷線時 API 會取消這個工作（plan_stream.py）。
"""
from __future__ import annotations

import logging

from pydantic import ValidationError
from rq import get_current_job
from rq.timeouts import JobTimeoutException

from ..db import SessionLocal
from ..planner import plan
from ..progress import Publisher
from ..schemas import PlanRequest
from ..services import PlanError, load_plan_context

log = logging.getLogger(__name__)


def run_plan(request: dict, job_id: str | None = None) -> dict | None:
    """排出方案並推送。回傳值只給測試用（RQ 不保存；API 和 MCP 都從 Pub/Sub 拿結果）。"""
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
    except JobTimeoutException:
        pub.error("TIMEOUT", "規劃超過時間上限，請稍後再試")
        raise
    except Exception as exc:  # 外部服務或程式出錯：通知前端後讓 RQ 記成失敗
        log.exception("規劃失敗")
        pub.error("UPSTREAM_ERROR", f"規劃時發生錯誤：{type(exc).__name__}")
        raise
    result = {"job_id": job_id, **result}
    pub.result(result)
    return result

"""送出規劃、追蹤進度（規格書 5.5、5.7、6.4）：REST 的 SSE 和 MCP 的進度通知共用。

1. 搶 active:plan:{IP}（值是 job_id，10 分鐘過期）：同一個 IP 同時只能有一個規劃 → 搶不到是 PLAN_IN_PROGRESS
2. rl:api:plan:{IP}：每個 IP 每小時 10 次 → 超過是 RATE_LIMITED
3. 先訂閱 plan:{job_id}:events、推送 queued，再排進 plans 佇列：事件不會漏，順序也不會亂
4. 轉送 worker 推送的事件，直到 result 或 error；排隊時順位改變就再推一次 queued
5. 結束時：沒收到 worker 的結果（使用者斷線、逾時）就取消工作，排隊中的移出佇列，執行中的用 RQ 停止；
   最後釋放 active:plan:{IP}

結果只經過 Pub/Sub，不寫進 Redis 的任何 key（RQ 的 result_ttl=0）。
"""
from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio
import redis.asyncio as aredis
from rq.command import send_stop_job_command
from rq.exceptions import InvalidJobOperation, NoSuchJobError
from rq.job import Job, JobStatus
from starlette.concurrency import run_in_threadpool

from .config import settings
from .jobs import PLAN_TIMEOUT_S, PLANS, queue
from .jobs.plan_job import run_plan
from .progress import Publisher, channel
from .redis_utils import get_redis, get_rq_redis
from .redis_utils.lock import RedisLock
from .redis_utils.ratelimit import SlidingWindow
from .schemas import PlanRequest

log = logging.getLogger(__name__)

SLOT_TTL_MS = PLAN_TIMEOUT_S * 1000  # active:plan:{IP} 10 分鐘過期：API 當掉也不會永遠卡住
PLAN_WINDOW_S = 60 * 60
POLL_S = 1.0  # 多久沒有事件就回報一次「沒事」（呼叫端拿來送心跳、檢查斷線）
STATUS_EVERY_S = 2.0  # 多久查一次工作狀態（排隊順位、worker 有沒有掛掉）
GRACE_S = 3.0  # 工作已經結束卻還沒收到結果時，再等一下 Pub/Sub 的訊息
FAILURE_TTL_S = 24 * 60 * 60
ENDED = (JobStatus.FINISHED, JobStatus.FAILED, JobStatus.STOPPED, JobStatus.CANCELED)
WAITING = (JobStatus.QUEUED, JobStatus.DEFERRED, JobStatus.SCHEDULED, JobStatus.CREATED)


class PlanRejected(Exception):
    """規劃送不出去：PLAN_IN_PROGRESS（409）或 RATE_LIMITED（429）。"""

    def __init__(self, code: str, message: str, retry_after_s: int | None = None):
        super().__init__(message)
        self.code, self.message, self.retry_after_s = code, message, retry_after_s


def reserve(ip: str) -> RedisLock:
    """搶這個 IP 的規劃名額，回傳的鎖值就是這次的 job_id。"""
    client = get_redis()
    slot = RedisLock(f"active:plan:{ip}", SLOT_TTL_MS, client, token=uuid.uuid4().hex)
    if not slot.acquire():
        raise PlanRejected("PLAN_IN_PROGRESS", "你已經有一個規劃在進行，等它完成再送出")
    decision = SlidingWindow(f"rl:api:plan:{ip}", settings.api_plan_limit_per_hour, PLAN_WINDOW_S, client).try_acquire()
    if not decision.allowed:
        slot.release()
        raise PlanRejected("RATE_LIMITED", "送出規劃的次數超過每小時上限，請稍後再試",
                           max(1, math.ceil(decision.retry_after_ms / 1000)))
    return slot


def enqueue(request: dict, job_id: str) -> tuple[Job, int]:
    """排進 plans 佇列，回傳（工作, 排隊順位）。"""
    q = queue(PLANS)
    position = q.count + 1
    Publisher(job_id).queued(position)  # 排進佇列前先推送，worker 的進度一定在它後面
    job = q.enqueue(run_plan, request, job_id=job_id, job_timeout=PLAN_TIMEOUT_S, ttl=PLAN_TIMEOUT_S,
                    result_ttl=0, failure_ttl=FAILURE_TTL_S)
    return job, position


def job_state(job: Job) -> tuple[JobStatus | None, int | None]:
    """（狀態, 排隊順位）。工作已經被刪掉（例如成功後 result_ttl=0）時狀態是 None。"""
    try:
        status = job.get_status(refresh=True)
    except (InvalidJobOperation, NoSuchJobError):
        return None, None
    if status != JobStatus.QUEUED:
        return status, None
    index = job.get_position()
    return status, None if index is None else index + 1


def cancel(job: Job) -> None:
    """排隊中的移出佇列；執行中的請 worker 停止（只有會 fork 的 worker 停得下來）。"""
    status, _ = job_state(job)
    try:
        if status in WAITING:
            job.cancel()
        elif status == JobStatus.STARTED:
            send_stop_job_command(get_rq_redis(), job.id)
    except (InvalidJobOperation, NoSuchJobError):
        pass  # 剛好結束了
    except Exception:  # 取消失敗不影響使用者：工作跑完，結果沒人收而已
        log.warning("取消規劃 %s 失敗", job.id, exc_info=True)


class PlanRun:
    """一次規劃：start() 送出，events() 依序產生事件，close() 收尾。"""

    def __init__(self, slot: RedisLock, client: aredis.Redis, pubsub, job: Job, position: int):
        self.slot, self.client, self.pubsub, self.job = slot, client, pubsub, job
        self.position = position  # 最近一次推送的排隊順位
        self.done = False  # 收到 worker 的 result 或 error

    @property
    def job_id(self) -> str:
        return self.job.id

    @classmethod
    async def start(cls, req: PlanRequest, ip: str) -> PlanRun:
        """搶名額 → 訂閱 → 排進佇列。PlanRejected、Redis 錯誤直接往外丟（API 轉成 409／429／503）。"""
        slot = await run_in_threadpool(reserve, ip)
        client = aredis.Redis.from_url(settings.redis_url, decode_responses=True)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(channel(slot.token))
            job, position = await run_in_threadpool(enqueue, req.model_dump(mode="json"), slot.token)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await pubsub.aclose()
                await client.aclose()
                await run_in_threadpool(slot.release)
            raise
        return cls(slot, client, pubsub, job, position)

    async def events(self, is_disconnected: Callable[[], Awaitable[bool]] | None = None,
                     timeout_s: float = PLAN_TIMEOUT_S) -> AsyncIterator[dict | None]:
        """產生 {"event": …, "data": …}，到 result 或 error 為止；None 代表 POLL_S 秒內沒有事件。"""
        clock = time.monotonic
        started = last_check = clock()
        ended_at: float | None = None
        while True:
            message = await self.pubsub.get_message(ignore_subscribe_messages=True, timeout=POLL_S)
            if message is not None:
                event = json.loads(message["data"])
                if event["event"] in ("result", "error"):
                    self.done = True
                yield event
                if self.done:
                    return
                continue
            if is_disconnected is not None and await is_disconnected():
                return
            now = clock()
            if now - started > timeout_s:
                yield {"event": "error", "data": {"code": "TIMEOUT", "message": "規劃超過時間上限，請稍後再試"}}
                return
            if ended_at is not None:
                if now - ended_at > GRACE_S:  # worker 掛掉、工作被刪：不會有結果了
                    yield {"event": "error", "data": {"code": "UPSTREAM_ERROR", "message": "規劃中斷了，請重新送出"}}
                    return
            elif now - last_check >= STATUS_EVERY_S:
                last_check = now
                status, position = await run_in_threadpool(job_state, self.job)
                if status is None or status in ENDED:
                    ended_at = now
                elif position is not None and position != self.position:
                    self.position = position
                    await run_in_threadpool(Publisher(self.job_id).queued, position)
            yield None

    async def close(self) -> None:
        """一定要呼叫（放在 finally）：沒收到結果就取消工作，再釋放名額、關閉連線。"""
        with anyio.CancelScope(shield=True):
            if not self.done:
                await run_in_threadpool(cancel, self.job)
            await run_in_threadpool(self.slot.release)
            await self.pubsub.aclose()
            await self.client.aclose()

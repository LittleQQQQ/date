"""規劃進度推播（規格書 5.7）：worker 透過 Redis Pub/Sub 推送，API 轉成 SSE 推給前端。

- 頻道 plan:{job_id}:events：每個事件是 JSON {"event": "queued" | "progress" | "result" | "error", "data": {...}}
- plan:{job_id}:status：目前狀態（state、stage、done、total），1 小時過期；**不含結果**
- 結果只透過 Pub/Sub 傳一次，不寫進 Redis 的任何 key（規格書 5.7、6.5：結果不存）
"""
from __future__ import annotations

import json

import redis

from .redis_utils import get_redis

STATUS_TTL_S = 60 * 60
STAGES = ("loading_data", "building_candidates", "routing", "waiting_quota", "fetching_places", "judging", "llm_fallback")


def channel(job_id: str) -> str:
    return f"plan:{job_id}:events"


def status_key(job_id: str) -> str:
    return f"plan:{job_id}:status"


class Publisher:
    def __init__(self, job_id: str, client: redis.Redis | None = None):
        self.job_id = job_id
        self.client = client or get_redis()

    def _send(self, event: str, data: dict, status: dict | None = None) -> None:
        pipe = self.client.pipeline()
        if status is not None:
            pipe.hset(status_key(self.job_id), mapping={k: str(v) for k, v in status.items()})
            pipe.expire(status_key(self.job_id), STATUS_TTL_S)
        pipe.publish(channel(self.job_id), json.dumps({"event": event, "data": data}, ensure_ascii=False))
        pipe.execute()

    def queued(self, position: int) -> None:
        self._send("queued", {"job_id": self.job_id, "position": position}, {"state": "queued", "position": position})

    def progress(self, stage: str, done: int = 0, total: int = 0) -> None:
        self._send("progress", {"stage": stage, "done": done, "total": total},
                   {"state": "running", "stage": stage, "done": done, "total": total})

    def result(self, result: dict) -> None:
        self._send("result", result, {"state": "done"})

    def error(self, code: str, message: str) -> None:
        self._send("error", {"code": code, "message": message}, {"state": "error", "code": code})

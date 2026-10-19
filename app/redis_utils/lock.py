"""分散式鎖（規格書 5.4）。

- 取得：SET key {隨機值} NX PX {毫秒}
- 延長、釋放：用 Lua script 比對值相同才做，避免延長或刪到別人的鎖（例如自己的鎖已經過期、被別人搶走）
- 沒搶到鎖的一方自己決定怎麼辦：排程工作直接跳過，換 TDX token 的則等一下再讀
"""
from __future__ import annotations

import threading
import uuid

import redis

from . import get_redis

_EXTEND = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisLock:
    def __init__(self, key: str, ttl_ms: int, client: redis.Redis | None = None):
        self.key = key  # 例如 lock:job:refresh_daily、lock:tdx:token
        self.ttl_ms = ttl_ms
        self.client = client or get_redis()
        self.token = uuid.uuid4().hex
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def acquire(self) -> bool:
        return bool(self.client.set(self.key, self.token, nx=True, px=self.ttl_ms))

    def extend(self) -> bool:
        """還是自己的鎖才延長；回傳 False 代表鎖已經不是自己的了。"""
        return bool(self.client.eval(_EXTEND, 1, self.key, self.token, self.ttl_ms))

    def release(self) -> bool:
        self.stop_heartbeat()
        return bool(self.client.eval(_RELEASE, 1, self.key, self.token))

    def start_heartbeat(self, interval_s: float) -> None:
        """工作跑很久時，每 interval_s 秒延長一次（規格書：鎖 10 分鐘、每 3 分鐘延長）。"""
        self._stop.clear()

        def beat():
            while not self._stop.wait(interval_s):
                if not self.extend():
                    return

        self._heartbeat = threading.Thread(target=beat, name=f"lock-heartbeat:{self.key}", daemon=True)
        self._heartbeat.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=1)
            self._heartbeat = None

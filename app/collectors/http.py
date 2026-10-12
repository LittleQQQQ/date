"""抓網頁的共用工具：限速、重試、快取。

- 同一個網站每秒最多一次請求（min_interval），避免造成對方負擔。
- 連線失敗、429、5xx 會等一下再試（預設 2 秒、4 秒，由 backoff 決定），最多 retries 次；其他 4xx 直接失敗。
- cache_days > 0 的網址存在 data/cache/，有效期間內不重抓（地圖頁、電影頁這種很少變的頁面）。
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..config import ROOT_DIR

CACHE_DIR = ROOT_DIR / "data" / "cache"
USER_AGENT = "date-planner/0.1 (student project; +https://github.com/LittleQQQQ/date)"
RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    """抓不到這一頁（重試後仍失敗，或網站回 4xx）。"""


class Fetcher:
    def __init__(
        self,
        cache_dir: Path | None = CACHE_DIR,
        min_interval: float = 1.0,
        retries: int = 3,
        timeout: float = 30.0,
        backoff: float = 2.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cache_dir = cache_dir
        self.min_interval = min_interval
        self.retries = retries
        self.backoff = backoff
        self.client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        self.sleep = sleep
        self.clock = clock
        self._last: dict[str, float] = {}

    def get(self, url: str, cache_days: float = 0) -> str:
        if cache_days > 0 and (cached := self._read_cache(url, cache_days)) is not None:
            return cached
        text = self._request("GET", url)
        if cache_days > 0:
            self._write_cache(url, text)
        return text

    def post(self, url: str, data: dict[str, str]) -> str:
        return self._request("POST", url, data=data)

    def forget(self, url: str) -> None:
        """丟掉這個網址的快取。抓到的頁面內容不對時用，免得壞頁面被留到快取過期。"""
        if self.cache_dir is not None:
            self._cache_path(url).unlink(missing_ok=True)

    def _request(self, method: str, url: str, **kwargs) -> str:
        last_error = ""
        for attempt in range(self.retries):
            self._wait_turn(url)
            try:
                resp = self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 400:
                    return resp.text
                if resp.status_code not in RETRY_STATUS:
                    raise FetchError(f"{url} 回應 {resp.status_code}")
                last_error = f"回應 {resp.status_code}"
            if attempt < self.retries - 1:
                self.sleep(self.backoff * 2**attempt)
        raise FetchError(f"{url} 重試 {self.retries} 次仍失敗：{last_error}")

    def _wait_turn(self, url: str) -> None:
        host = urlsplit(url).netloc
        if host in self._last:
            gap = self.min_interval - (self.clock() - self._last[host])
            if gap > 0:
                self.sleep(gap)
        self._last[host] = self.clock()

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()}.html"

    def _read_cache(self, url: str, days: float) -> str | None:
        if self.cache_dir is None:
            return None
        path = self._cache_path(url)
        if not path.exists() or time.time() - path.stat().st_mtime > days * 86400:
            return None
        return path.read_text(encoding="utf-8")

    def _write_cache(self, url: str, text: str) -> None:
        if self.cache_dir is None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(url).write_text(text, encoding="utf-8")

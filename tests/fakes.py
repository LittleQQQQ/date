"""測試用的假抓網頁工具：網址對到樣本，不連網。"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Union

from app.collectors.http import FetchError

SAMPLES = Path(__file__).parent / "samples"

PageSource = Union[str, Path, Exception, Callable[[dict | None], str], list]


class FakeFetcher:
    """pages：網址 → 內容。內容可以是字串、樣本檔路徑、要丟出的例外、
    callable(data)（POST 時拿到表單資料），或上述元素的清單（每次呼叫取出第一個）。"""

    def __init__(self, pages: dict[str, PageSource]):
        self.pages = pages
        self.requested: list[str] = []
        self.posts: list[dict] = []
        self.forgotten: list[str] = []

    def get(self, url: str, cache_days: float = 0) -> str:
        return self._serve(url, None)

    def post(self, url: str, data: dict[str, str]) -> str:
        self.posts.append(data)
        return self._serve(url, data)

    def forget(self, url: str) -> None:
        self.forgotten.append(url)

    def _serve(self, url: str, data: dict | None) -> str:
        self.requested.append(url)
        page = self.pages.get(url)
        if isinstance(page, list):
            page = page.pop(0) if page else None
        if page is None:
            raise FetchError(f"{url} 回應 404")
        if isinstance(page, Exception):
            raise page
        if isinstance(page, Path):
            return page.read_text(encoding="utf-8")
        if callable(page):
            return page(data)
        return page

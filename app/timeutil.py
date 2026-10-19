"""時間工具：分鐘數與 "HH:MM" 互轉，以及中文日期、時間的解析。

台灣沒有日光節約時間，直接用 UTC+8（Windows 上的 Python 沒有內建時區資料庫）。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))
WEEKDAY_NAMES = "一二三四五六日"
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def now_tw() -> datetime:
    """現在的台灣時間，帶時區。資料表的時間欄位都是 timestamptz，不帶時區的時間會被當成 UTC 存進去。"""
    return datetime.now(TW)


def today_tw() -> date:
    return datetime.now(TW).date()


def to_min(hhmm: str) -> int:
    m = _HHMM.match(hhmm.strip())
    if not m or int(m.group(2)) > 59:
        raise ValueError(f"時間格式應為 HH:MM：{hhmm!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def hhmm(minutes: int) -> str:
    """分鐘數 → "HH:MM"，跨午夜保留 24:30 這種寫法（給程式用）。"""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def fmt(minutes: int) -> str:
    """分鐘數 → 顯示用文字，跨午夜寫成「次日 00:30」。"""
    h, m = divmod(minutes, 60)
    text = f"{h % 24:02d}:{m:02d}"
    return f"次日 {text}" if h >= 24 else text


def weekday_label(d: date) -> str:
    return f"{d.month}/{d.day}（{WEEKDAY_NAMES[d.weekday()]}）"


# ---------- 日期 ----------

_DATE_ISO = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DATE_MD = re.compile(r"(\d{1,2})\s*(?:/|月)\s*(\d{1,2})\s*(?:日|號)?")
_WEEKDAY = re.compile(r"(下下|下|這|本)?\s*(?:週|周|星期|禮拜)([一二三四五六日天])")
_RELATIVE = (("大後天", 3), ("後天", 2), ("明天", 1), ("明晚", 1), ("今天", 0), ("今晚", 0))


def resolve_date(text: str, today: date) -> date | None:
    """把「週六」「明天」「10/17」這類說法換成日期；看不懂就回傳 None。"""
    if not text:
        return None
    if m := _DATE_ISO.search(text):
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if m := _DATE_MD.search(text):
        month, day = int(m.group(1)), int(m.group(2))
        try:
            found = date(today.year, month, day)
            if found < today - timedelta(days=30):  # 已經過了一個月以上，當成明年
                found = date(today.year + 1, month, day)
            return found
        except ValueError:
            return None
    for word, offset in _RELATIVE:
        if word in text:
            return today + timedelta(days=offset)
    if m := _WEEKDAY.search(text):
        prefix, ch = m.groups()
        target = WEEKDAY_NAMES.index("日" if ch == "天" else ch)
        found = today - timedelta(days=today.weekday()) + timedelta(days=target)
        if prefix == "下":
            found += timedelta(days=7)
        elif prefix == "下下":
            found += timedelta(days=14)
        elif found < today:
            found += timedelta(days=7)
        return found
    if "週末" in text or "周末" in text:
        return today + timedelta(days=(5 - today.weekday()) % 7)
    return None


# ---------- 時刻 ----------

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CLOCK_RE = re.compile(
    r"(?<![早晚快慢多少差有])"
    r"(早上|上午|中午|下午|傍晚|晚上|半夜|凌晨)?\s*"
    r"(\d{1,2}|[一二兩三四五六七八九十]{1,3})\s*"
    r"(?:[:：](\d{2})|點(?:\s*(半|\d{1,2})\s*分?)?)"
)


def _cn_number(s: str) -> int:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN_DIGITS[s[1]]
    if s.endswith("十"):
        return _CN_DIGITS[s[0]] * 10
    if "十" in s:
        tens, ones = s.split("十")
        return _CN_DIGITS[tens] * 10 + _CN_DIGITS[ones]
    return _CN_DIGITS[s]


def _guess_period(hour: int, kind: str) -> int:
    """沒講上午下午時，依情境猜：到家時間多半是晚上或半夜，出門時間 1～8 點多半是下午。"""
    if kind == "home_by":
        if hour < 5:
            return hour + 24
        if hour < 12:
            return hour + 12
    elif kind == "depart_after" and 1 <= hour < 9:
        return hour + 12
    return hour


def clock_from_match(m: re.Match, kind: str) -> str | None:
    period, hour_text, colon_min, point_min = m.groups()
    try:
        hour = _cn_number(hour_text)
    except (KeyError, IndexError):
        return None
    minute = int(colon_min) if colon_min else 30 if point_min == "半" else int(point_min) if point_min else 0
    if hour > 29 or minute > 59:
        return None
    if period in ("下午", "傍晚", "晚上") and hour < 12:
        hour += 12
    elif period == "中午" and hour < 6:
        hour += 12
    elif period in ("半夜", "凌晨"):
        if kind == "home_by" and hour < 6:
            hour += 24
    elif period is None:
        hour = _guess_period(hour, kind)
    return f"{hour:02d}:{minute:02d}"


def parse_clock(text: str, kind: str) -> str | None:
    """從一段文字找出第一個時刻，例如「22:30」「晚上十點半」。kind 是 home_by 或 depart_after。"""
    if not text:
        return None
    m = CLOCK_RE.search(text)
    return clock_from_match(m, kind) if m else None

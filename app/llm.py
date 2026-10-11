"""LLM（本機 Ollama）：只負責「語言」——把一句話轉成條件，以及把結果寫成口語摘要。

- 時間、金額、行程都由程式計算，LLM 只能引用，不能改。
- 日期、出發站、片名這類規則就能準確抓到的欄位，以規則為準；LLM 補其他欄位。
- Ollama 沒開或失敗時，改用規則解析和範本摘要，Demo 不會卡住。
"""
from __future__ import annotations

import datetime as dt
import json
import re

import httpx

from .config import settings
from .jev.mock import normalize_title
from .jev.questions import AUDIO, CUISINE, SCREEN
from .timeutil import CLOCK_RE, WEEKDAY_NAMES, clock_from_match, parse_clock, resolve_date


class LLMUnavailable(Exception):
    pass


async def _ollama_chat(messages: list[dict], schema: dict | None = None, temperature: float = 0.0) -> str:
    if not settings.ollama_url:
        raise LLMUnavailable("沒有設定 OLLAMA_URL")
    payload: dict = {
        "model": settings.ollama_model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
        "keep_alive": "10m",
    }
    if schema:
        payload["format"] = schema
    try:
        async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
            resp = await client.post(f"{settings.ollama_url.rstrip('/')}/api/chat", json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMUnavailable(f"連不上 Ollama：{type(exc).__name__}") from exc
    return resp.json()["message"]["content"]


async def status() -> dict:
    info = {"model": settings.ollama_model, "available": False, "model_installed": False}
    if not settings.ollama_url:
        return info
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{settings.ollama_url.rstrip('/')}/api/tags")
            resp.raise_for_status()
    except httpx.HTTPError:
        return info
    names = {m.get("name", "") for m in resp.json().get("models", [])}
    info["available"] = True
    info["model_installed"] = settings.ollama_model in names
    return info


# ---------- 規則解析 ----------

CUISINE_HINTS = {
    "日式": ("日式", "日本料理", "拉麵", "壽司", "丼"),
    "韓式": ("韓式", "韓國", "炸雞"),
    "中式": ("中式", "中餐", "點心", "小籠包", "熱炒"),
    "義式／西式": ("義式", "義大利", "西式", "西餐", "牛排", "燉飯"),
    "美式": ("美式", "漢堡"),
    "泰式／南洋": ("泰式", "泰國", "南洋", "越南"),
    "火鍋／燒肉": ("火鍋", "麻辣鍋", "燒肉"),
    "咖啡／甜點": ("咖啡", "甜點", "下午茶"),
    "酒館": ("酒館", "酒吧", "調酒", "喝一杯", "小酌"),
}
_BUDGET = re.compile(r"(?:預算|每人|一人|一個人)[^0-9]{0,4}(\d{3,5})")
_QUOTED_TITLE = re.compile(r"《(.+?)》|「(.+?)」")
_ORIGIN_ALIASES = {"北車": "台北車站"}


def _match_origin(text: str, meta: dict) -> str | None:
    text = text.replace("臺", "台")
    for alias, name in _ORIGIN_ALIASES.items():
        if alias in text:
            return name
    for name in sorted((o["name"] for o in meta["origins"]), key=len, reverse=True):
        if name in text:
            return name
    return None


def _match_movie(text: str, meta: dict) -> str | None:
    norm = normalize_title(text)
    hits = [t for t in meta["movies"] if normalize_title(t) and normalize_title(t) in norm]
    if hits:
        return max(hits, key=lambda t: len(normalize_title(t)))
    if m := _QUOTED_TITLE.search(text):
        return (m.group(1) or m.group(2)).strip()
    return None


def _find_times(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for m in CLOCK_RE.finditer(text):
        after = text[m.end() : m.end() + 6]
        if "前" in after[:3] and any(w in after for w in ("家", "回", "到")):
            kind = "home_by"
        elif any(w in after for w in ("出門", "出發")) or after.startswith(("後", "以後", "之後")):
            kind = "depart_after"
        else:
            continue
        if kind not in found and (value := clock_from_match(m, kind)):
            found[kind] = value
    return found


def rule_parse(text: str, today: dt.date, meta: dict) -> dict:
    out: dict = {}
    if d := resolve_date(text, today):
        out["date"] = d.isoformat()
    if origin := _match_origin(text, meta):
        out["origin"] = origin
    if movie := _match_movie(text, meta):
        out["movie_query"] = movie
    out.update(_find_times(text))
    if screens := [s for s in ("IMAX", "4DX") if s.lower() in text.lower()]:
        out["screen_types"] = screens
    if any(w in text for w in ("配音", "中文版", "國語版")):
        out["audio"] = "國語配音"
    elif "原音" in text:
        out["audio"] = "原音"
    if cuisines := [c for c, words in CUISINE_HINTS.items() if any(w in text for w in words)]:
        out["cuisines"] = cuisines
    if m := _BUDGET.search(text):
        out["budget_per_person"] = int(m.group(1))
    if any(w in text for w in ("先吃", "吃完再看", "吃飽再看", "吃飯再看")):
        out["meal_order"] = "before"
    elif any(w in text for w in ("看完再吃", "散場", "看完去吃", "看完吃", "看完電影再吃")):
        out["meal_order"] = "after"
    return out


# ---------- LLM 解析 ----------

PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "date_text": {"type": "string"},
        "origin": {"type": "string"},
        "depart_after": {"type": "string"},
        "home_by": {"type": "string"},
        "movie_query": {"type": "string"},
        "screen_types": {"type": "array", "items": {"type": "string", "enum": list(SCREEN.options)}},
        "audio": {"type": "string", "enum": ["", *AUDIO.options]},
        "cuisines": {"type": "array", "items": {"type": "string", "enum": list(CUISINE.options)}},
        "budget_per_person": {"type": "integer"},
        "meal_order": {"type": "string", "enum": ["any", "after", "before"]},
    },
    "required": [
        "date_text", "origin", "depart_after", "home_by", "movie_query",
        "screen_types", "audio", "cuisines", "budget_per_person", "meal_order",
    ],
}

_EXAMPLE_IN = "明天晚上想看星際迴響，從台北車站出發，11點前要回家，想吃日式"
_EXAMPLE_OUT = {
    "date_text": "明天", "origin": "台北車站", "depart_after": "", "home_by": "23:00", "movie_query": "星際迴響",
    "screen_types": [], "audio": "", "cuisines": ["日式"], "budget_per_person": 0, "meal_order": "any",
}


def _parse_prompt(today: dt.date) -> str:
    return (
        "你是約會行程助理的需求解析器。把使用者的一句話轉成 JSON。"
        "只填使用者有明確提到的資訊；沒提到的字串填 \"\"、數字填 0、清單填 []，不要自己猜。\n"
        f"今天是 {today.isoformat()}（星期{WEEKDAY_NAMES[today.weekday()]}）。\n"
        "欄位：\n"
        "- date_text：使用者說的日期原文，例如「週六」「明天」「10/17」\n"
        "- origin：出發的捷運站名，例如「板橋」\n"
        "- depart_after：最早幾點可以出門，24 小時制 HH:MM\n"
        "- home_by：最晚幾點要到家，24 小時制 HH:MM（晚上十點半 → 22:30）\n"
        "- movie_query：想看的電影片名\n"
        f"- screen_types：想看的影廳，只能用 {'、'.join(SCREEN.options)}\n"
        f"- audio：{'或'.join(AUDIO.options)}\n"
        f"- cuisines：想吃的料理，只能用：{'、'.join(CUISINE.options)}\n"
        "- budget_per_person：每人預算（新台幣整數）\n"
        "- meal_order：看完電影再吃飯填 after，先吃飯再看電影填 before，沒說就填 any"
    )


def _from_llm(raw: dict, today: dt.date, meta: dict) -> dict:
    out: dict = {}
    if d := resolve_date(str(raw.get("date_text") or ""), today):
        out["date"] = d.isoformat()
    if origin := _match_origin(str(raw.get("origin") or ""), meta):
        out["origin"] = origin
    for kind in ("depart_after", "home_by"):
        if value := parse_clock(str(raw.get(kind) or ""), kind):
            out[kind] = value
    if movie := str(raw.get("movie_query") or "").strip("《》「」 "):
        out["movie_query"] = movie
    if screens := [s for s in raw.get("screen_types") or [] if s in SCREEN.options]:
        out["screen_types"] = screens
    if raw.get("audio") in AUDIO.options:
        out["audio"] = raw["audio"]
    if cuisines := [c for c in raw.get("cuisines") or [] if c in CUISINE.options]:
        out["cuisines"] = cuisines
    if isinstance(raw.get("budget_per_person"), int) and raw["budget_per_person"] > 0:
        out["budget_per_person"] = raw["budget_per_person"]
    if raw.get("meal_order") in ("after", "before"):
        out["meal_order"] = raw["meal_order"]
    return out


RULES_FIRST = ("date", "origin", "movie_query")
LIST_FIELDS = ("screen_types", "cuisines")


async def parse_request(text: str, today: dt.date, meta: dict) -> tuple[dict, dict]:
    """回傳（PlanRequest 的欄位, 解析資訊）。解析資訊會顯示在網頁上，讓使用者知道每個欄位從哪來。"""
    rules = rule_parse(text, today, meta)
    llm_fields: dict = {}
    llm_error = None
    try:
        content = await _ollama_chat(
            [
                {"role": "system", "content": _parse_prompt(today)},
                {"role": "user", "content": _EXAMPLE_IN},
                {"role": "assistant", "content": json.dumps(_EXAMPLE_OUT, ensure_ascii=False)},
                {"role": "user", "content": text},
            ],
            schema=PARSE_SCHEMA,
        )
        llm_fields = _from_llm(json.loads(content), today, meta)
    except (LLMUnavailable, json.JSONDecodeError, KeyError, TypeError) as exc:
        llm_error = str(exc) or type(exc).__name__

    fields: dict = {"date": today.isoformat()}
    sources: dict[str, str] = {}
    for key in {*rules, *llm_fields}:
        if key in LIST_FIELDS:
            merged = list(dict.fromkeys([*rules.get(key, []), *llm_fields.get(key, [])]))
            fields[key] = merged
            sources[key] = "規則＋LLM" if key in rules and key in llm_fields else ("規則" if key in rules else "LLM")
            continue
        first, second = (rules, llm_fields) if key in RULES_FIRST else (llm_fields, rules)
        if key in first:
            fields[key], sources[key] = first[key], "規則" if first is rules else "LLM"
        else:
            fields[key], sources[key] = second[key], "規則" if second is rules else "LLM"

    info = {
        "mode": "llm" if llm_error is None else "rules",
        "model": settings.ollama_model,
        "llm_error": llm_error,
        "sources": sources,
    }
    return fields, info


# ---------- 摘要 ----------


def template_summary(result: dict) -> str:
    plans = result.get("plans") or []
    if not plans:
        reasons = "、".join(f"{r['reason']}（{r['count']}）" for r in (result.get("rejections") or [])[:3])
        text = "找不到接得起來的行程。"
        if reasons:
            text += f"主要卡在：{reasons}。"
        if result.get("confirmations"):
            text += "有幾個 Jev 沒把握的地方，確認後可能會有新方案。"
        return text
    best = plans[0]
    m, r = best["movie"], best["restaurant"]
    feasible = result.get("stats", {}).get("feasible", len(plans))
    return (
        f"在 {feasible} 個接得起來的組合裡挑了 {len(plans)} 個方案。"
        f"最推薦：{m['start']} 在{m['cinema']}看《{m['title']}》（{m['label']}），{best['order_text']}，"
        f"到{r['name']}用餐，{best['leave_home']} 出門、{best['arrive_home']} 到家，每人約 {best['cost_per_person']} 元。"
    )


_NUMBER = re.compile(r"\d+(?::\d{2})?")


def _invented_numbers(text: str, facts: str) -> set[str]:
    """摘要裡出現、但程式沒提供的數字或時間。小模型偶爾會自己編，例如「約需 34 分鐘」。"""
    allowed = set(_NUMBER.findall(facts.replace(",", "")))
    return set(_NUMBER.findall(text.replace(",", ""))) - allowed


async def summarize(result: dict) -> tuple[str, str]:
    """回傳（摘要文字, 來源 llm / template）。LLM 摘要裡有程式沒提供的數字，就改用範本。"""
    plans = result.get("plans") or []
    fallback = template_summary(result)
    if not plans:
        return fallback, "template"
    facts = []
    for p in plans:
        m, r = p["movie"], p["restaurant"]
        line = (
            f"{p['label']}：{p['order_text']}；{m['start']} 在{m['cinema']}看《{m['title']}》（{m['label']}）；"
            f"餐廳 {r['name']}（{r['cuisine']}，約會適合度：{r['date_fit']}）；"
            f"{p['leave_home']} 出門、{p['arrive_home']} 到家；每人約 {p['cost_per_person']} 元"
        )
        if p["flags"]:
            line += "；注意：" + "、".join(p["flags"])
        facts.append(line)
    facts_text = "\n".join(facts)
    try:
        text = await _ollama_chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是約會行程助理。請用繁體中文（台灣用語）寫 3 到 4 句話，向使用者介紹下面的方案並說明推薦理由。"
                        "只能使用提供的店名、時間和金額，不可以編造任何新的數字、時間或店家。不要用條列，不要用 Markdown。"
                    ),
                },
                {"role": "user", "content": facts_text},
            ],
            temperature=0.3,
        )
    except LLMUnavailable:
        return fallback, "template"
    text = text.strip()
    if not text or _invented_numbers(text, facts_text):
        return fallback, "template"
    return text, "llm"

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
    "中式／台式": ("中式", "中餐", "台式", "台菜", "小吃", "點心", "小籠包", "熱炒"),
    "義式／西式": ("義式", "義大利", "西式", "西餐", "牛排", "燉飯"),
    "美式": ("美式", "漢堡"),
    "泰式／南洋": ("泰式", "泰國", "南洋", "越南"),
    "火鍋／燒肉": ("火鍋", "麻辣鍋", "燒肉"),
    "咖啡／甜點": ("咖啡", "甜點", "下午茶"),
    "酒館": ("酒館", "酒吧", "調酒", "喝一杯", "小酌"),
}
GENRE_HINTS = {
    "動作": ("動作",), "愛情": ("愛情", "浪漫的片"), "科幻": ("科幻",), "動畫": ("動畫", "卡通"),
    "喜劇": ("喜劇", "搞笑"), "劇情": ("劇情片",), "恐怖": ("恐怖", "驚悚"),
}
DIET_HINTS = {"不吃辣": ("不吃辣", "不能吃辣", "怕辣"), "素食": ("素食", "吃素", "蔬食"), "不吃牛": ("不吃牛",)}
_BUDGET = re.compile(r"(?:預算|每人|一人|一個人)[^0-9]{0,4}(\d{3,5})")
_QUOTED_TITLE = re.compile(r"《(.+?)》|「(.+?)」")
_ORIGIN_ALIASES = {"北車": "台北車站"}


def _match_origin(text: str, meta: dict) -> str | None:
    """回傳出發站代碼。站名重複（例如台北、台中都有市政府）時，看句子裡有沒有提到縣市。"""
    text = text.replace("臺", "台")
    for alias, name in _ORIGIN_ALIASES.items():
        text = text.replace(alias, name)
    for origin in sorted(meta["origins"], key=lambda o: len(o["name"]), reverse=True):
        if origin["name"] not in text:
            continue
        same_name = [o for o in meta["origins"] if o["name"] == origin["name"]]
        if len(same_name) == 1:
            return origin["id"]
        for o in same_name:
            if o["city"] in text or o["city"].removesuffix("市") in text:
                return o["id"]
    return None


def _match_area(text: str, meta: dict, origin_id: str | None) -> str | None:
    """回傳商圈代碼。「從板橋出發去信義」同時提到兩個商圈時，排除出發站所在的那個。"""
    text = text.replace("臺", "台")
    hits = [a for a in meta["areas"] if a["name"] in text]
    if len(hits) > 1 and origin_id:
        origin_name = next((o["name"] for o in meta["origins"] if o["id"] == origin_id), "")
        hits = [a for a in hits if a["name"] != origin_name] or hits
    return max(hits, key=lambda a: len(a["name"]))["id"] if hits else None


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
    if area := _match_area(text, meta, out.get("origin")):
        out["area"] = area
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
    if genre := next((g for g, words in GENRE_HINTS.items() if any(w in text for w in words)), None):
        out["genre"] = genre
    if diet := next((d for d, words in DIET_HINTS.items() if any(w in text for w in words)), None):
        out["diet"] = diet
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


RULES_FIRST = ("date", "origin", "area", "movie_query")
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


def _trip_phrase(direction: str, trip: dict) -> str:
    return f"{direction}步行" if trip["choice"] == "步行" else f"{direction}搭{trip['choice']}"


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
    go, back = best["transit"]["go"], best["transit"]["back"]
    fares = go["fare"] + back["fare"]
    cost = f"每人約 {best['cost_per_person']} 元" + (f"（含來回車資約 {fares} 元）" if fares else "")
    return (
        f"從 {feasible} 個接得起來的組合裡，Jev 選出 {len(plans)} 個方案。"
        + "".join(result.get("notes") or [])
        + f"Jev 首選：{m['start']} 在{m['cinema']}看《{m['title']}》（{m['label']}），{best['order_text']}，"
        f"到{r['name']}用餐；{_trip_phrase('去程', go)}、{_trip_phrase('回程', back)}，"
        f"{best['leave_home']} 出門、{best['arrive_home']} 到家，"
        f"{cost}。"
    )


# 阿拉伯數字，或「四點」「八百八十元」「兩小時」這種中文數字寫法；
# 「十分適合」「輕鬆一點」「一個人」是一般用語，不算
_NUMBER_LIKE = re.compile(
    r"\d"
    r"|(?:[二兩三四五六七八九十]|[一二兩三四五六七八九十]{2,})點"
    r"|[零一二兩三四五六七八九十百千]+(?:元|塊錢)"
    r"|[一二兩三四五六七八九十半]+個?(?:小時|鐘頭)"
)


def has_numbers(text: str) -> bool:
    """推薦理由裡不能有任何數字。小模型轉述時間、金額常常出錯，例如把 14:21 寫成「下午四點」。"""
    return bool(_NUMBER_LIKE.search(text))


async def summarize(result: dict) -> tuple[str, str]:
    """回傳（摘要文字, 來源 llm / template）。

    時間、金額、店名一律由程式用範本寫；LLM 只補一兩句沒有數字的推薦理由，有數字就整句不用。
    """
    plans = result.get("plans") or []
    facts_summary = template_summary(result)
    if not plans:
        return facts_summary, "template"
    facts = []
    for p in plans:
        m, r = p["movie"], p["restaurant"]
        movie = f"在{m['cinema']}看《{m['title']}》（{m['label']}）"
        meal = f"到{r['name']}吃飯（{r['cuisine']}，約會適合度：{r['date_fit']}）"
        middle = f"{movie}，看完再{meal}" if p["order"] == "movie_first" else f"先{meal}，再{movie}"
        line = f"{p['label']}：{middle}"
        if p["flags"]:
            line += "；注意：" + "、".join(p["flags"])
        facts.append(line)
    try:
        reason = await _ollama_chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是約會行程助理。請用繁體中文（台灣用語）寫一到兩句推薦理由："
                        "說明「Jev 首選」的方案為什麼適合約會，以及其他方案適合什麼樣的情況。"
                        "絕對不要寫任何時間、金額或數字，也不要提到下面沒有的店家。不要用條列，不要用 Markdown。"
                    ),
                },
                {"role": "user", "content": "\n".join(facts)},
            ],
            temperature=0.3,
        )
    except LLMUnavailable:
        return facts_summary, "template"
    reason = reason.strip()
    if not reason or has_numbers(reason):
        return facts_summary, "template"
    return f"{facts_summary}{reason}", "llm"

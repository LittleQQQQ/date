"""LLM（本機 Ollama）：負責「語言」——把一句話轉成條件、把結果寫成口語摘要；Jev 沒把握時也幫忙判斷（judge）。

- 時間、金額、行程都由程式計算，LLM 只能引用，不能改。
- 日期、出發站、片名、時間、預算這類規則就能準確抓到的欄位，以規則為準；LLM 補其他欄位。
- Ollama 沒開或失敗時，改用規則解析和範本摘要，Demo 不會卡住。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable

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


# ---------- Jev 沒把握時幫忙判斷 ----------

JUDGE_TIMEOUT = 20.0  # 秒；排程當下呼叫，等太久不如請使用者確認
_JUDGE_SYSTEM = (
    "你幫忙回答封閉式的判斷題。只根據使用者給的資料判斷，不要猜資料裡沒有的事；"
    "從選項中選一個最可能的答案，用 JSON 回答 {\"choice\": 選項}。"
)


def judge(question: str, options: list[str], text: str) -> str | None:
    """請 LLM 回答一題封閉式問題（和 Jev 讀同一段文字、選同樣的選項）。回傳選項之一；沒開、失敗、答非所問都回 None。

    排程是同步程式（在 MCP 工具裡執行），所以這裡用同步的 HTTP 呼叫。
    """
    if not settings.ollama_url:
        return None
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "options": {"temperature": 0},
        "keep_alive": "10m",
        "format": {"type": "object", "properties": {"choice": {"type": "string", "enum": list(options)}}, "required": ["choice"]},
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": f"問題：{question}\n選項：{'、'.join(options)}\n資料：\n{text}"},
        ],
    }
    try:
        resp = httpx.post(f"{settings.ollama_url.rstrip('/')}/api/chat", json=payload, timeout=JUDGE_TIMEOUT)
        resp.raise_for_status()
        choice = json.loads(resp.json()["message"]["content"])["choice"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return choice if choice in options else None


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
# 句子裡交代出發站的地方：「從○○出發／去／到」，或沒有「從」的「○○出發」
_ORIGIN_PHRASES = (
    re.compile(r"從([^，。,；;]+?)(?:出發|去|到)"),
    re.compile(r"([^，。,；;]+?)出發"),
)


def _short_name(name: str) -> str:
    """站名「（」前面的部分：八德（介壽大智路口）→ 八德。"""
    return name.split("（")[0]


def _origin_in(segment: str, text: str, meta: dict, skip_areas: bool = False) -> str | None:
    """在 segment 裡找出發站（全名或簡稱，名字長的先比）。站名重複（例如台北、台中都有市政府）時，看 text 有沒有提到縣市。
    skip_areas：站名只出現在更長的商圈名裡（「桃園區」裡的「桃園」）不算。"""
    names = [(o["name"], o) for o in meta["origins"]]
    names += [(short, o) for o in meta["origins"] if (short := _short_name(o["name"])) not in ("", o["name"])]
    for name, origin in sorted(names, key=lambda n: len(n[0]), reverse=True):
        haystack = segment
        if skip_areas:
            for area in meta["areas"]:
                if name in area["name"] and name != area["name"]:
                    haystack = haystack.replace(area["name"], "\0" * len(area["name"]))
        if name not in haystack:
            continue
        same_name = [o for n, o in names if n == name]
        if len(same_name) == 1:
            return origin["id"]
        for o in same_name:
            if o["city"] in text or o["city"].removesuffix("市") in text:
                return o["id"]
    return None


def _match_origin(text: str, meta: dict) -> str | None:
    """回傳出發站代碼。先看「從○○出發」這類說法裡的站（「從八德出發去中壢」的出發站是八德），沒有再比對整句。"""
    text = text.replace("臺", "台")
    for alias, name in _ORIGIN_ALIASES.items():
        text = text.replace(alias, name)
    for phrase in _ORIGIN_PHRASES:
        for m in phrase.finditer(text):
            if origin := _origin_in(m.group(1), text, meta):
                return origin
    return _origin_in(text, text, meta, skip_areas=True)


def _match_area(text: str, meta: dict, origin_id: str | None) -> str | None:
    """回傳商圈代碼。「從板橋出發去信義」同時提到兩個商圈時，排除出發站所在的那個（站名或簡稱同名的商圈）。"""
    text = text.replace("臺", "台")
    hits = [a for a in meta["areas"] if a["name"] in text]
    if len(hits) > 1 and origin_id:
        origin_name = next((o["name"] for o in meta["origins"] if o["id"] == origin_id), "")
        hits = [a for a in hits if a["name"] not in (origin_name, _short_name(origin_name))] or hits
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


# 規則有抓到就用規則的；時間和金額也算在內，因為小模型轉述數字常出錯（「十點前到家」寫成 20:00）
RULES_FIRST = ("date", "origin", "area", "movie_query", "depart_after", "home_by", "budget_per_person")
LIST_FIELDS = ("screen_types", "cuisines")


def _default_places(fields: dict, sources: dict, meta: dict) -> None:
    """句子沒提到商圈或出發站時，從開放範圍內挑（PlanRequest 的預設值不一定在開放範圍內）。
    商圈：出發站服務的商圈，沒有就挑同縣市第一個；出發站：商圈所在縣市的第一個（大站排前面，和網頁預設一樣）。
    開放範圍內還沒有商圈有場次時不挑，讓呼叫端回報資料還沒好。"""
    areas, origins = meta["areas"], meta["origins"]
    if "area" not in fields and areas:
        origin = next((o for o in origins if o["id"] == fields.get("origin")), None)
        if origin and any(a["id"] == origin.get("area") for a in areas):
            fields["area"] = origin["area"]
        else:
            same_city = [a for a in areas if origin and a["city"] == origin["city"]]
            fields["area"] = (same_city or areas)[0]["id"]
        sources["area"] = "預設"
    if "origin" not in fields and origins:
        city = next((a["city"] for a in areas if a["id"] == fields.get("area")), None)
        fields["origin"] = ([o for o in origins if o["city"] == city] or origins)[0]["id"]
        sources["origin"] = "預設"


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
    _default_places(fields, sources, meta)

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


def has_numbers(text: str, names: Iterable[str] = ()) -> bool:
    """推薦理由裡不能有任何數字。小模型轉述時間、金額常常出錯，例如把 14:21 寫成「下午四點」。
    names 是我們給 LLM 的店名、片名這類名稱，名稱裡本來就有的數字（定食8、粽邪4、3D）照抄不算。"""
    for name in sorted(names, key=len, reverse=True):
        text = text.replace(name, "\0")
    return bool(_NUMBER_LIKE.search(text))


def _plan_names(plans: list[dict]) -> set[str]:
    names: set[str] = set()
    for p in plans:
        m, r = p["movie"], p["restaurant"]
        names |= {r["name"], m["title"], m["cinema"], m["label"]}
    return {n for n in names if n}


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
        movie = f"在{m['cinema']}看《{m['title']}》（版本：{m['label']}）"  # 標明是版本，免得「水影威尼斯」被當成餐廳
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
    if not reason or has_numbers(reason, _plan_names(plans)):
        return facts_summary, "template"
    return f"{facts_summary}{reason}", "llm"

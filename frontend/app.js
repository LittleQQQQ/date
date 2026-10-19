"use strict";

// 約會行程規劃的網頁（規格書第 6 節的 API）。
// 送出規劃是 POST /api/plans，回應是 SSE 串流：EventSource 只支援 GET，所以用 fetch 一段一段讀。

const $ = (sel, root = document) => root.querySelector(sel);
const state = { areas: [], coverage: null, locations: new Map(), controller: null, request: null };

const TIMES = range(10 * 60, 21 * 60, 30); // 10:00–21:00，每 30 分鐘
const BUDGETS = [1000, 1500, 2000, 2500, 3000, 4000, 5000, 8000];
const DEFAULT_TIME = "17:00";
const DEFAULT_BUDGET = 2000;
const STAGES = {
  loading_data: "讀取場次和餐廳",
  building_candidates: "組合場次和餐廳",
  routing: "計算交通",
  waiting_quota: "等待 Jev 額度",
  fetching_places: "查詢 Google 店家資料",
  judging: "Jev 打分數",
  llm_fallback: "Jev 沒把握，交給 LLM",
};
const ERROR_TITLES = {
  VALIDATION_ERROR: "條件有誤",
  NOT_FOUND: "找不到資料",
  PLAN_IN_PROGRESS: "請稍等",
  RATE_LIMITED: "送出太多次了",
  SERVICE_UNAVAILABLE: "服務暫時無法使用",
  NO_SHOWTIMES: "沒有符合條件的場次",
  NO_CANDIDATES: "排不出接得上的行程",
  TIMEOUT: "規劃超過時間",
  UPSTREAM_ERROR: "規劃時發生錯誤",
};
const PRICE_LEVELS = { INEXPENSIVE: "平價", MODERATE: "中等價位", EXPENSIVE: "偏高價位", VERY_EXPENSIVE: "高價位" };
const WEEKDAYS = "日一二三四五六";

class ApiError extends Error {
  constructor(code, message, retryAfter = null) {
    super(message);
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

// ---------- 小工具 ----------

function range(start, end, step) {
  const out = [];
  for (let m = start; m <= end; m += step) out.push(m);
  return out;
}

function hhmm(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function money(n) {
  return `$${Number(n).toLocaleString("zh-TW")}`;
}

function options(list) {
  return list.map(([value, label, disabled]) => `<option value="${esc(value)}"${disabled ? " disabled" : ""}>${esc(label)}</option>`).join("");
}

// 台灣時間的今天（YYYY-MM-DD）和現在幾分（從午夜算起）
function taiwanNow() {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date()).map((p) => [p.type, p.value]),
  );
  return { date: `${parts.year}-${parts.month}-${parts.day}`, minutes: Number(parts.hour) * 60 + Number(parts.minute) };
}

function addDays(isoDate, days) {
  const d = new Date(`${isoDate}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function dateLabel(isoDate, today) {
  const d = new Date(`${isoDate}T00:00:00Z`);
  const base = `${d.getUTCMonth() + 1}/${d.getUTCDate()}（${WEEKDAYS[d.getUTCDay()]}）`;
  if (isoDate === today) return `今天 ${base}`;
  if (isoDate === addDays(today, 1)) return `明天 ${base}`;
  return base;
}

// API 回傳的時間是 ISO 8601（+08:00）；跨過午夜的顯示「次日」
function clock(iso, day) {
  const text = iso.slice(11, 16);
  return iso.slice(0, 10) === day ? text : `次日 ${text}`;
}

async function getJSON(path) {
  const res = await fetch(path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error?.code ?? `HTTP_${res.status}`, data.error?.message ?? `伺服器錯誤（${res.status}）`, res.headers.get("Retry-After"));
  return data;
}

// ---------- 表單 ----------

const form = () => $("#plan-form");
const field = (name) => form().elements[name];

function coverageOf(code) {
  return state.coverage?.areas.find((a) => a.code === code);
}

function setupForm(areas, categories) {
  field("area").innerHTML = options(areas.map((a) => {
    const c = coverageOf(a.code);
    return [a.code, c?.showtime_to ? a.name : `${a.name}（還沒有場次資料）`, !c?.showtime_to];
  }));
  const first = areas.find((a) => coverageOf(a.code)?.showtime_to);
  if (first) field("area").value = first.code;

  const byKind = (kind) => categories.filter((c) => c.kind === kind).map((c) => [c.code, c.label]);
  field("movie_genre").innerHTML = options([["", "不限"], ...byKind("movie_genre")]);
  field("cuisine").innerHTML = options([["", "不限"], ...byKind("cuisine")]);
  field("party_size").innerHTML = options(range(1, 10, 1).map((n) => [n, `${n} 人`]));
  field("party_size").value = "2";
  field("budget_total").innerHTML = options(BUDGETS.map((b) => [b, money(b)]));
  field("budget_total").value = String(DEFAULT_BUDGET);
}

async function onAreaChange() {
  const code = field("area").value;
  if (!code) return;
  if (!state.locations.has(code)) state.locations.set(code, (await getJSON(`/api/areas/${encodeURIComponent(code)}/locations`)).locations);
  const list = state.locations.get(code).map((l) => [l.id, l.name]);
  field("origin_location_id").innerHTML = options(list);
  field("destination_location_id").innerHTML = options(list);
  updateDates();
}

function updateDates() {
  const c = coverageOf(field("area").value);
  const now = taiwanNow();
  const dates = [];
  if (c?.showtime_to) {
    for (let d = c.showtime_from > now.date ? c.showtime_from : now.date; d <= c.showtime_to; d = addDays(d, 1)) {
      // 今天已經沒有可以選的出發時間就不列
      if (d !== now.date || TIMES.some((t) => t > now.minutes)) dates.push(d);
    }
  }
  const keep = field("date").value;
  field("date").innerHTML = options(dates.map((d) => [d, dateLabel(d, now.date)]));
  if (dates.includes(keep)) field("date").value = keep;
  $("#coverage-hint").textContent = c?.showtime_to
    ? `這一區的場次資料：${dateLabel(c.showtime_from > now.date ? c.showtime_from : now.date, now.date)}到 ${dateLabel(c.showtime_to, now.date)}。`
    : "這一區還沒有場次資料。";
  updateTimes();
}

function updateTimes() {
  const now = taiwanNow();
  const keep = field("time").value || DEFAULT_TIME;
  const times = TIMES.filter((t) => field("date").value !== now.date || t > now.minutes).map(hhmm);
  field("time").innerHTML = options(times.map((t) => [t, t]));
  field("time").value = times.includes(keep) ? keep : (times[0] ?? "");
}

function readForm() {
  const f = form().elements;
  return {
    area: f.area.value,
    origin_location_id: Number(f.origin_location_id.value),
    destination_location_id: Number(f.destination_location_id.value),
    order: f.order.value,
    depart_at: `${f.date.value}T${f.time.value}:00+08:00`,
    movie_genre: f.movie_genre.value || null,
    cuisine: f.cuisine.value || null,
    party_size: Number(f.party_size.value),
    budget_total: Number(f.budget_total.value),
  };
}

function formProblem(req) {
  if (!req.area) return "請選區域";
  if (!req.origin_location_id || !req.destination_location_id) return "請選出發地和結束後要去的地方";
  if (!form().elements.date.value || !form().elements.time.value) return "這一區目前沒有可以選的日期或時間";
  return "";
}

function showFormError(message) {
  const el = $("#form-error");
  el.textContent = message;
  el.hidden = !message;
}

// ---------- 規劃（SSE） ----------

async function* readEvents(body) {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value.replace(/\r\n/g, "\n");
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith(":")) continue; // 心跳
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) yield { event, data: JSON.parse(data.join("\n")) };
    }
  }
}

async function runPlan(event) {
  event.preventDefault();
  const req = readForm();
  const problem = formProblem(req);
  showFormError(problem);
  if (problem) return;

  state.controller?.abort();
  const controller = new AbortController();
  state.controller = controller;
  state.request = req;
  setBusy(true);
  $("#results").innerHTML = "";
  renderProgress("送出中…");
  try {
    const res = await fetch("/api/plans", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(req), signal: controller.signal,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new ApiError(data.error?.code ?? `HTTP_${res.status}`, data.error?.message ?? `伺服器錯誤（${res.status}）`, res.headers.get("Retry-After"));
    }
    for await (const { event: name, data } of readEvents(res.body)) {
      if (name === "queued") renderProgress(data.position > 1 ? `排隊中，前面還有 ${data.position - 1} 個規劃` : "排隊中，馬上開始");
      else if (name === "progress") renderProgress(STAGES[data.stage] ?? data.stage, data.done, data.total);
      else if (name === "result") return renderResult(data, req);
      else if (name === "error") throw new ApiError(data.code, data.message);
    }
    throw new ApiError("UPSTREAM_ERROR", "連線中斷了，請重新送出");
  } catch (err) {
    if (state.controller !== controller) return; // 已經送出新的規劃
    if (err.name === "AbortError") renderNotice("已取消這次規劃。");
    else renderError(err);
  } finally {
    if (state.controller === controller) {
      state.controller = null;
      setBusy(false);
    }
  }
}

function cancelPlan() {
  state.controller?.abort(); // 關閉連線，伺服器會取消排隊中或執行中的工作
}

function setBusy(busy) {
  $("#submit").disabled = busy;
  $("#submit").textContent = busy ? "規劃中…" : "幫我排行程";
  $("#cancel").hidden = !busy;
}

// ---------- 畫面 ----------

function renderProgress(text, done = 0, total = 0) {
  const pct = total ? Math.round((done / total) * 100) : null;
  $("#progress").innerHTML = `
    <div class="card progress">
      <p class="loading">${esc(text)}${total ? `（${done}/${total}）` : ""}</p>
      <div class="meter${pct === null ? " busy" : ""}"><i style="width:${pct ?? 100}%"></i></div>
    </div>`;
}

function renderNotice(text) {
  $("#progress").innerHTML = `<div class="card"><p class="loading">${esc(text)}</p></div>`;
}

function renderError(err) {
  const title = ERROR_TITLES[err.code] ?? "出了點問題";
  const retry = err.retryAfter ? `<p class="hint">大約 ${Math.ceil(Number(err.retryAfter) / 60)} 分鐘後可以再試。</p>` : "";
  $("#progress").innerHTML = `
    <div class="card error-card">
      <h2>${esc(title)}</h2>
      <p>${esc(err.message)}</p>${retry}
    </div>`;
}

function renderStatus() {
  const items = [];
  const withData = (state.coverage?.areas ?? []).filter((a) => a.showtime_to);
  items.push(`<li class="chip"><span class="dot ${withData.length ? "on" : "off"}"></span>有場次的區域 ${withData.length} / ${state.coverage?.areas.length ?? 0}</li>`);
  const source = state.coverage?.restaurant_source === "google" ? "Google 地圖＋OSM" : "OpenStreetMap";
  items.push(`<li class="chip"><span class="dot on"></span>餐廳資料：${esc(source)}</li>`);
  $("#status").innerHTML = items.join("");
}

function transitStep(item, day) {
  const fare = item.fare_per_person ? `，每人 ${money(item.fare_per_person)}${item.fare_estimated ? "（估計）" : ""}` : "";
  const rides = (item.rides ?? []).map((r) =>
    `<span>${esc(r.line)}：${esc(r.from)} ${clock(r.board_at, day)} 上車 → ${esc(r.to)} ${clock(r.alight_at, day)} 下車</span>`).join("");
  return `
    <li class="step-transit">
      <time>${clock(item.depart_at, day)}</time>
      <div><strong>${esc(item.summary)}</strong>
        <span>${esc(item.from)} → ${esc(item.to)}，${clock(item.arrive_at, day)} 到${fare}</span>${rides}</div>
    </li>`;
}

function movieStep(item, day) {
  return `
    <li class="step-movie">
      <time>${clock(item.starts_at, day)}</time>
      <div><strong>${esc(item.title)}</strong>
        <span>${esc(item.cinema)}・${esc(item.version)}・${clock(item.ends_at, day)} 散場</span>
        <span>開演前 15 分鐘（${clock(item.arrive_at, day)}）到影城，每人票價 ${money(item.ticket_price_per_person)}</span></div>
    </li>`;
}

function mealStep(item, day) {
  const hours = `${item.opening_hours_today}${item.hours_estimated && !item.opening_hours_today.includes("估計") ? "（估計）" : ""}`;
  const google = item.source === "google";
  return `
    <li class="step-meal">
      <time>${clock(item.seat_at, day)}</time>
      <div><strong>${esc(item.name)}</strong>
        ${item.address ? `<span>${esc(item.address)}</span>` : ""}
        <span>今天營業 ${esc(hours)}・${esc(PRICE_LEVELS[item.price_level] ?? item.price_level)}，每人約 ${money(item.price_per_person_estimate)}</span>
        <span>用餐到 ${clock(item.leave_at, day)}</span>
        <span class="attribution${google ? " google" : ""}">${google ? "資料來源：Google Maps" : esc(item.attribution)}</span></div>
    </li>`;
}

function bar(label, value) {
  if (value === null || value === undefined) return "";
  const pct = Math.round(value * 100);
  return `<li><span class="q">${esc(label)}</span><div class="row"><div class="bar"><i style="width:${pct}%"></i></div><b>${pct}</b></div></li>`;
}

function renderPlan(p, req) {
  const day = req.depart_at.slice(0, 10);
  const movie = p.timeline.find((i) => i.type === "movie");
  const meal = p.timeline.find((i) => i.type === "meal");
  const steps = p.timeline.map((i) => (i.type === "transit" ? transitStep(i, day) : i.type === "movie" ? movieStep(i, day) : mealStep(i, day))).join("");
  const c = p.cost;
  const over = c.total_estimate > req.budget_total;
  const who = p.decided_by === "llm" ? "Jev＋LLM 備援" : p.decided_by === "jev" ? "Jev" : "沒有評分";
  return `
    <article class="card plan">
      <div>
        <span class="pill">第 ${p.rank} 名${p.score === null ? "" : `・${Math.round(p.score * 100)} 分`}</span>
        <h3>${esc(movie.title)} ＋ ${esc(meal.name)}</h3>
        <p class="sub">${clock(p.depart_at, day)} 出發，${clock(p.arrive_at, day)} 到達・等待時間合計 ${p.idle_minutes} 分鐘・評分：${esc(who)}</p>
      </div>
      <dl class="facts">
        <div><dt>電影票</dt><dd>${money(c.tickets_total)}</dd></div>
        <div><dt>交通${c.transit_estimated ? "（估計）" : ""}</dt><dd>${money(c.transit_total)}</dd></div>
        <div><dt>餐費（估計）</dt><dd>${money(c.meal_total_estimate)}</dd></div>
        <div><dt>總計</dt><dd class="${over ? "over" : ""}">${money(c.total_estimate)}</dd></div>
      </dl>
      ${over ? `<p class="flag">比預算 ${money(req.budget_total)} 多 ${money(c.total_estimate - req.budget_total)}</p>` : ""}
      <ol class="timeline">${steps}</ol>
      <details class="jev">
        <summary>Jev 的判斷</summary>
        <ul>
          ${bar("時間安排", p.judgment.time_fit)}
          ${bar("交通方便", p.judgment.transit_ease)}
          ${bar("在預算內的機率", p.judgment.budget_within_prob)}
        </ul>
      </details>
    </article>`;
}

function renderResult(result, req) {
  $("#progress").innerHTML = "";
  const s = result.stats;
  const google = result.restaurant_source === "google";
  $("#results").innerHTML = `
    <div class="card summary">
      <p>找到 ${result.plans.length} 個行程，依 Jev 的分數排序。</p>
      <p class="by">從 ${s.showtimes} 個場次、${s.restaurants} 家餐廳，共 ${s.combinations.toLocaleString("zh-TW")} 種組合中挑選。${google ? "餐廳資料包含 Google 地圖。" : ""}</p>
    </div>
    <div class="plans">${result.plans.map((p) => renderPlan(p, req)).join("")}</div>`;
}

// ---------- 啟動 ----------

async function init() {
  form().addEventListener("submit", runPlan);
  $("#cancel").addEventListener("click", cancelPlan);
  field("area").addEventListener("change", () => onAreaChange().catch(renderError));
  field("date").addEventListener("change", updateTimes);
  try {
    const [areas, coverage, categories] = await Promise.all([getJSON("/api/areas"), getJSON("/api/coverage"), getJSON("/api/categories")]);
    state.areas = areas.areas;
    state.coverage = coverage;
    setupForm(state.areas, categories.categories);
    renderStatus();
    await onAreaChange();
  } catch (err) {
    renderError(err);
  }
}

init();

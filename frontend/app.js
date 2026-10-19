"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = { meta: null, overrides: {}, busy: false };

// 下拉選單的選項
const DEPART_TIMES = range(10 * 60, 21 * 60, 30); // 10:00–21:00
const HOME_TIMES = range(18 * 60, 25 * 60, 30); // 18:00–次日 01:00
const BUDGETS = [0, 800, 1000, 1500, 2000, 3000, 5000];
const WALK_LIMITS = [5, 8, 10, 12, 15, 20];
const DEFAULTS = { depart_after: "16:00", home_by: "23:00", budget_per_person: "0", max_walk_min: "12" };

function range(start, end, step) {
  const out = [];
  for (let m = start; m <= end; m += step) out.push(m);
  return out;
}

function hhmm(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

function clockLabel(minutes) {
  const text = hhmm(minutes % (24 * 60));
  return minutes >= 24 * 60 ? `次日 ${text}` : text;
}

function toMinutes(value) {
  const [h, m] = value.split(":").map(Number);
  return h * 60 + m;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    const message = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("；") : "";
    throw new Error(message || `伺服器錯誤（${res.status}）`);
  }
  return data;
}

// ---------- 表單 ----------

function options(list) {
  return list.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
}

function stationLabel(name) {
  return name.endsWith("站") ? name : `${name}站`;
}

function cityOptions(hasData) {
  // 開放範圍內的縣市依地區分組；沒有資料的縣市可以看到但不能選
  const groups = new Map();
  for (const city of state.meta.cities) {
    if (!groups.has(city.region)) groups.set(city.region, []);
    const ok = hasData(city);
    groups.get(city.region).push(
      `<option value="${esc(city.name)}"${ok ? "" : " disabled"}>${esc(city.name)}${ok ? "" : "（尚無資料）"}</option>`,
    );
  }
  return [...groups].map(([region, opts]) => `<optgroup label="${esc(region)}">${opts.join("")}</optgroup>`).join("");
}

function findCity(name) {
  return state.meta.cities.find((c) => c.name === name);
}

function selectedArea() {
  const form = $("#plan-form");
  return findCity(form.city.value)?.areas.find((a) => a.id === form.area.value);
}

function refill(select, list, fallback = "") {
  // 換選項時，原本選的值還在就保留
  const current = select.value;
  select.innerHTML = options(list);
  const values = list.map(([value]) => String(value));
  select.value = values.includes(current) ? current : values.includes(fallback) ? fallback : values[0] ?? "";
}

function setupForm(meta) {
  const form = $("#plan-form");
  form.city.innerHTML = cityOptions((c) => c.areas.length > 0);
  form.origin_city.innerHTML = cityOptions((c) => c.origins.length > 0);
  form.depart_after.innerHTML = options(DEPART_TIMES.map((m) => [hhmm(m), clockLabel(m)]));
  form.home_by.innerHTML = options(HOME_TIMES.map((m) => [hhmm(m), clockLabel(m)]));
  form.screen_type.innerHTML = options([["", "不限"], ...meta.options.screen_types.map((s) => [s, s])]);
  form.audio.innerHTML = options([["", "不限"], ...meta.options.audio.map((a) => [a, a])]);
  form.cuisine.innerHTML = options([["", "不限"], ...meta.options.cuisines.map((c) => [c, c])]);
  form.genre.innerHTML = options([["", "不限"], ...meta.options.genres.map((g) => [g, g])]);
  form.diet.innerHTML = options([["", "沒有"], ...meta.options.diets.map((d) => [d, d])]);
  form.budget_per_person.innerHTML = options(BUDGETS.map((b) => [b, b ? `${b.toLocaleString()} 元以內` : "不限"]));
  form.max_walk_min.innerHTML = options(WALK_LIMITS.map((w) => [w, `${w} 分鐘以內`]));
  for (const [name, value] of Object.entries(DEFAULTS)) form[name].value = value;

  const firstCity = meta.cities.find((c) => c.areas.length > 0);
  form.city.value = firstCity ? firstCity.name : "";
  onCityChange();
}

function onCityChange() {
  // 換約會縣市時，出發縣市也先跟著換（有出發站的話）；要跨縣市再自己改
  const form = $("#plan-form");
  const city = findCity(form.city.value);
  refill(form.area, (city?.areas ?? []).map((a) => [a.id, a.name]));
  if (city && city.origins.length) form.origin_city.value = city.name;
  updateOrigins();
  updateDates();
}

function updateOrigins() {
  const form = $("#plan-form");
  const city = findCity(form.origin_city.value);
  refill(form.origin, (city?.origins ?? []).map((o) => [o.id, stationLabel(o.name)]));
}

function updateDates() {
  const form = $("#plan-form");
  refill(form.date, (selectedArea()?.dates ?? []).map((d) => [d.date, d.label]));
  updateMovies();
}

function updateMovies() {
  // 片單跟著商圈和日期變：只列出那天有上映的電影
  const form = $("#plan-form");
  const day = selectedArea()?.dates.find((d) => d.date === form.date.value);
  refill(form.movie_query, [["", "交給 Jev 決定"], ...(day ? day.movies : []).map((t) => [t, t])]);
}

function readForm() {
  const form = $("#plan-form");
  return {
    area: form.area.value,
    date: form.date.value,
    origin: form.origin.value,
    depart_after: form.depart_after.value,
    home_by: form.home_by.value,
    movie_query: form.movie_query.value,
    genre: form.genre.value,
    diet: form.diet.value,
    screen_types: form.screen_type.value ? [form.screen_type.value] : [],
    audio: form.audio.value,
    cuisines: form.cuisine.value ? [form.cuisine.value] : [],
    budget_per_person: Number(form.budget_per_person.value),
    meal_order: form.meal_order.value,
    max_walk_min: Number(form.max_walk_min.value),
    overrides: state.overrides,
  };
}

function validate(req) {
  if (!req.area) return "請選一個有資料的縣市和商圈";
  if (!req.origin) return "請選出發站";
  if (!req.date) return "這個商圈目前沒有可以選的場次日期";
  if (toMinutes(req.home_by) <= toMinutes(req.depart_after)) return "最晚到家時間要晚於最早出門時間";
  return "";
}

// ---------- 狀態列 ----------

const SOURCE_NAMES = { atmovies: "開眼", osm: "OSM" };

function shortTime(text) {
  // "2026-10-12 06:01" → "10/12 06:01"
  const [day, time] = String(text ?? "").split(" ");
  const [, month, date] = day.split("-");
  return month ? `${Number(month)}/${Number(date)} ${time}` : "";
}

function dataChip(meta, refresh) {
  if (refresh?.running) {
    return `<span class="dot"></span>資料更新中…（${esc(SOURCE_NAMES[refresh.running] ?? refresh.running)}）`;
  }
  if (meta.data.is_demo) return "資料：示意資料";
  const parts = meta.data.sources
    .filter((s) => s.source in SOURCE_NAMES)
    .map((s) => `${esc(s.label)} ${esc(shortTime(s.finished_at))}`);
  return parts.length ? `資料：${parts.join("・")}` : "資料：尚未抓取";
}

function renderStatus(meta, status) {
  const llm = status?.llm;
  const llmChip = llm === undefined
    ? `<span class="dot"></span>LLM ${esc(meta.llm.model)}：檢查中`
    : llm.available && llm.model_installed
      ? `<span class="dot on"></span>LLM ${esc(llm.model)}：已連線`
      : `<span class="dot off"></span>LLM ${esc(meta.llm.model)}：${llm.available ? "未安裝模型" : "未連線"}（摘要改用範本）`;
  const jev = meta.jev.mode === "api" ? "Jev：API" : "Jev：模擬模式";
  $("#status").innerHTML = [
    `<li class="chip"><span class="dot on"></span>${jev}</li>`,
    `<li class="chip">${llmChip}</li>`,
    `<li class="chip">${dataChip(meta, status?.refresh)}</li>`,
  ].join("");
  const google = meta.restaurants?.source === "google";
  const counts = google
    ? `目前有 ${meta.areas.length} 個商圈、${meta.counts.cinemas} 家影城的資料，餐廳在排程時即時查詢 Google 地圖。`
    : `目前有 ${meta.areas.length} 個商圈、${meta.counts.cinemas} 家影城、${meta.counts.restaurants} 家餐廳的資料。`;
  const restaurantCredit = google
    ? "Google Maps（查詢失敗時改用 © OpenStreetMap 貢獻者的資料，ODbL）"
    : "© OpenStreetMap 貢獻者（ODbL）";
  const credit = meta.data.is_demo
    ? "影城、餐廳、場次與交通班次目前為示意資料，僅供展示流程。"
    : `電影與場次：開眼電影網・餐廳：${restaurantCredit}・交通：估計值。`;
  $("#foot").innerHTML = `${counts}${credit}`;
}

// ---------- 第一次啟動：等爬蟲抓完 ----------

function renderWaiting(refresh) {
  const what = refresh.running === "atmovies" ? "正在抓取影城場次（第一次約 20 分鐘）" : "正在抓取餐廳資料（約 10～20 分鐘）";
  $("#results").innerHTML = `<section class="card summary"><p class="loading">第一次啟動，${what}，完成後會自動載入。</p></section>`;
}

function renderNoData(refresh) {
  const errors = Object.entries(refresh?.errors ?? {}).map(([s, e]) => `${SOURCE_NAMES[s] ?? s}：${e}`);
  const detail = errors.length ? `抓取失敗（${errors.join("；")}）。` : "";
  $("#results").innerHTML = `<section class="card summary"><p>目前還沒有場次資料。${esc(detail)}伺服器每天早上會自動再抓一次，也可以手動執行 python -m app.ingest --source atmovies。</p></section>`;
}

function watchRefresh(status) {
  // 還沒有場次時：正在抓就每 10 秒檢查一次，抓完重新載入；沒在抓就說明原因
  if (state.meta.data.has_showtimes) return;
  if (!status?.refresh?.running) {
    renderNoData(status?.refresh);
    return;
  }
  renderWaiting(status.refresh);
  const timer = setInterval(async () => {
    const s = await api("/api/status").catch(() => null);
    if (!s) return;
    renderStatus(state.meta, s);
    if (s.refresh.running) {
      renderWaiting(s.refresh);
    } else {
      clearInterval(timer);
      location.reload();
    }
  }, 10000);
}

// ---------- 結果 ----------

function renderSummary(result, summary, source) {
  const by = source === "llm" ? "時間與金額來自排程計算；最後的推薦理由由本機 LLM 撰寫" : "系統自動產生";
  const text = summary || (result.plans.length
    ? `從 ${result.stats.feasible} 個接得起來的組合裡，Jev 選出 ${result.plans.length} 個方案。${(result.notes || []).join("")}`
    : "找不到接得起來的行程，看看下面的原因，或放寬條件再試一次。");
  const note = result.restaurant_source?.note ? `<p class="notice">${esc(result.restaurant_source.note)}</p>` : "";
  return `<section class="card summary"><p>${esc(text)}</p>${note}<p class="by">${by}</p></section>`;
}

function confirmItem(item) {
  const pct = (p) => `${Math.round(p * 100)}%`;
  const buttons = item.options.map((opt, i) => {
    const prob = item.kind === "choice" ? `<small class="opt-prob">${pct(item.probs[i])}</small>` : "";
    return `<button type="button" class="opt${opt === item.jev_guess ? " guess" : ""}"
      data-key="${esc(item.key)}" data-value="${esc(opt)}">${esc(opt)}${prob}</button>`;
  });
  return `
    <div class="confirm-item">
      <p>${esc(item.question)}<small>Jev 猜「${esc(item.jev_guess)}」，信心 ${pct(item.confidence)}</small></p>
      <div class="opts">${buttons.join("")}</div>
    </div>`;
}

function renderConfirmations(items) {
  const filters = items.filter((i) => i.kind !== "choice");
  const choices = items.filter((i) => i.kind === "choice");
  let html = "";
  if (filters.length) {
    html += `
      <section class="card confirm">
        <h2>Jev 不太確定，幫忙確認一下</h2>
        <p class="hint">這些判斷 Jev 沒把握，所以相關的場次或餐廳先不排。選完會重新排程。</p>
        ${filters.map(confirmItem).join("")}
      </section>`;
  }
  if (choices.length) {
    html += `
      <section class="card confirm">
        <h2>Jev 的選擇沒有很肯定，想換換看嗎？</h2>
        <p class="hint">下面的方案已經照 Jev 的選擇排好；點其他選項會改用你選的重新排。</p>
        ${choices.map(confirmItem).join("")}
      </section>`;
  }
  return html;
}

function choiceRow(label, c, only = "只有這個選項排得進去") {
  const pct = Math.round(c.prob * 100);
  const who = c.source === "user" ? "（你指定或確認的）"
    : c.source === "program" ? `（${only}）`
    : c.source === "llm" ? (c.rank === 1 ? "（Jev 沒把握，LLM 選的）" : "（LLM 沒選這個，照 Jev 的順序）")
    : `（Jev 第 ${c.rank} 名）`;
  return `
    <li>
      <span class="q">${esc(label)}</span>
      <span class="row"><strong>${esc(c.choice)}${who}</strong><span class="bar"><i style="width:${pct}%"></i></span><span>${pct}%</span></span>
    </li>`;
}

function renderJev(p) {
  const threshold = state.meta?.jev.threshold ?? 0.6;
  const attributes = p.jev.map((a) => {
    const pct = Math.round(a.confidence * 100);
    const who = a.overridden ? "（你確認的）" : a.source === "llm" ? "（Jev 沒把握，LLM 判斷）" : "";
    const unsure = a.confidence < threshold && !a.overridden && a.source !== "llm";
    return `
      <li class="${unsure ? "low" : ""}">
        <span class="q">${esc(a.question)}</span>
        <span class="row"><strong>${esc(a.top)}${who}</strong><span class="bar"><i style="width:${pct}%"></i></span><span>${pct}%</span></span>
      </li>`;
  });
  return `
    <details class="jev"><summary>Jev 怎麼選、怎麼判斷的</summary>
      <h4>Jev 的選擇</h4>
      <ul>${choiceRow("看哪部片", p.jev_choice.movie)}${choiceRow("吃哪一家", p.jev_choice.restaurant)}${choiceRow("去程搭什麼", p.jev_choice.go, "只有這種走法接得上、付得起")}${choiceRow("回程搭什麼", p.jev_choice.back, "只有這種走法接得上、付得起")}</ul>
      <h4>屬性判斷（用來篩選與估算時間）</h4>
      <ul>${attributes.join("")}</ul>
    </details>`;
}

function renderPlan(p, restaurantSource) {
  const m = p.movie;
  const r = p.restaurant;
  const restaurantName = /^https?:\/\//.test(r.url ?? "")
    ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a>`
    : esc(r.name);
  // 餐廳資料來自 Google 時，依 Google 的規定要標示來源
  const credit = restaurantSource?.source === "google" ? `<p class="credit">餐廳資料：Google Maps</p>` : "";
  const steps = p.steps.map((s) => `
    <li class="step-${esc(s.kind)}">
      <time>${esc(s.time)}</time>
      <div><strong>${esc(s.title)}</strong>${s.detail ? `<span>${esc(s.detail)}</span>` : ""}</div>
    </li>`).join("");
  const flags = p.flags.length ? `<ul class="flags">${p.flags.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : "";
  const tips = p.tips.length ? `<p class="tips">${p.tips.map(esc).join("；")}</p>` : "";
  const estimates = p.estimates?.length
    ? `<ul class="flags">${p.estimates.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`
    : "";
  const b = p.cost_breakdown;
  const costLine = `<p class="cost-line">每人花費：電影票 $${b.movie.toLocaleString()}・餐費 $${b.meal.toLocaleString()}・去程車資 $${b.go.toLocaleString()}・回程車資 $${b.back.toLocaleString()}</p>`;
  return `
    <article class="card plan">
      <header>
        ${p.labels.map((l) => `<span class="pill">${esc(l)}</span>`).join(" ")}
        <h3>${esc(m.start)}《${esc(m.title)}》→ ${restaurantName}</h3>
        <p class="sub">${esc(m.genre ? `${m.genre}片・` : "")}${esc(m.cinema)}・${esc(m.label)}・${esc(p.order_text)}</p>
        <p class="jev-line">Jev 選片 ${Math.round(p.jev_choice.movie.prob * 100)}% × 選餐廳 ${Math.round(p.jev_choice.restaurant.prob * 100)}%
          ＝ 組合信心 ${Math.round(p.jev_confidence * 100)}%</p>
      </header>
      <dl class="facts">
        <div><dt>出門</dt><dd>${esc(p.leave_home)}</dd></div>
        <div><dt>到家</dt><dd>${esc(p.arrive_home)}</dd></div>
        <div><dt>每人</dt><dd>$${p.cost_per_person.toLocaleString()}</dd></div>
        <div><dt>最緊緩衝</dt><dd>${p.tightest_buffer_min} 分</dd></div>
      </dl>
      ${costLine}
      ${flags}${estimates}
      <ol class="timeline">${steps}</ol>
      ${tips}
      ${renderJev(p)}
      ${credit}
    </article>`;
}

function renderDiagnostics(result) {
  const filterLine = (label, f) => {
    const reasons = Object.entries(f.reasons).map(([k, v]) => `${k} ${v}`).join("、");
    return `<p>${label}：共 ${f.total}，留下 ${f.kept}${reasons ? `（${esc(reasons)}）` : ""}</p>`;
  };
  const rejections = result.rejections.length
    ? `<ul>${result.rejections.map((r) => `<li>${esc(r.reason)}：${r.count}</li>`).join("")}</ul>`
    : "<p>沒有被排除的組合。</p>";
  const assumptions = Object.entries(result.assumptions).map(([k, v]) => `<li>${esc(k)}：${esc(v)}</li>`).join("");
  return `
    <details class="card diag">
      <summary>排程過程（檢查了 ${result.stats.combinations_checked} 個組合，${result.stats.feasible} 個可行）</summary>
      <h3>篩選</h3>
      ${filterLine("場次", result.filters.showtimes)}
      ${filterLine("餐廳", result.filters.restaurants)}
      <h3>接不起來的原因</h3>
      ${rejections}
      <h3>計算假設</h3>
      <ul>${assumptions}</ul>
    </details>`;
}

function renderResult({ result, summary, summary_source: source }) {
  $("#results").innerHTML = [
    renderSummary(result, summary, source),
    renderConfirmations(result.confirmations),
    result.plans.length
      ? `<div class="plans">${result.plans.map((p) => renderPlan(p, result.restaurant_source)).join("")}</div>`
      : "",
    renderDiagnostics(result),
  ].join("");
}

function renderError(message) {
  $("#results").innerHTML = `<section class="card summary"><p>出錯了：${esc(message)}</p></section>`;
}

function setBusy(busy) {
  state.busy = busy;
  for (const btn of document.querySelectorAll("#plan-form button, .opt")) btn.disabled = busy;
  if (busy) {
    $("#results").innerHTML = `<section class="card summary"><p class="loading">排程中…（第一次呼叫本機 LLM 寫摘要可能要等十幾秒）</p></section>`;
  }
}

// ---------- 動作 ----------

async function runPlan() {
  const req = readForm();
  const error = $("#form-error");
  const problem = validate(req);
  if (problem) {
    error.textContent = problem;
    error.hidden = false;
    return;
  }
  error.hidden = true;
  setBusy(true);
  try {
    renderResult(await api("/api/plan", req));
  } catch (err) {
    renderError(err.message);
  } finally {
    setBusy(false);
  }
}

async function init() {
  try {
    state.meta = await api("/api/meta");
  } catch (err) {
    renderError(`無法載入設定：${err.message}`);
    return;
  }
  setupForm(state.meta);
  renderStatus(state.meta);
  api("/api/status")
    .then((s) => {
      renderStatus(state.meta, s);
      watchRefresh(s);
    })
    .catch(() => renderStatus(state.meta, { llm: { available: false } }));

  const form = $("#plan-form");
  form.city.addEventListener("change", onCityChange);
  form.area.addEventListener("change", updateDates);
  form.origin_city.addEventListener("change", updateOrigins);
  form.date.addEventListener("change", updateMovies);
  // 確認過的 Jev 答案綁在場次標示或餐廳上，跟條件無關，所以改條件時保留
  form.addEventListener("change", () => { $("#form-error").hidden = true; });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    runPlan();
  });
  $("#results").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-key]");
    if (!btn || state.busy) return;
    state.overrides = { ...state.overrides, [btn.dataset.key]: btn.dataset.value };
    runPlan();
  });
}

init();

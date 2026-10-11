"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = { meta: null, overrides: {}, busy: false };

// 下拉選單的選項
const DEPART_TIMES = range(10 * 60, 21 * 60, 30); // 10:00–21:00
const HOME_TIMES = range(18 * 60, 25 * 60, 30); // 18:00–次日 01:00
const BUDGETS = [0, 800, 1000, 1500, 2000, 3000];
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

function setupForm(meta) {
  const form = $("#plan-form");
  form.area.innerHTML = options(meta.areas.map((a) => [a, a]));
  form.date.innerHTML = options(meta.dates.map((d) => [d.date, d.label]));
  form.origin.innerHTML = options(meta.origins.map((o) => [o.name, `${o.name}站`]));
  form.depart_after.innerHTML = options(DEPART_TIMES.map((m) => [hhmm(m), clockLabel(m)]));
  form.home_by.innerHTML = options(HOME_TIMES.map((m) => [hhmm(m), clockLabel(m)]));
  form.screen_type.innerHTML = options([["", "不限"], ...meta.options.screen_types.map((s) => [s, s])]);
  form.audio.innerHTML = options([["", "不限"], ...meta.options.audio.map((a) => [a, a])]);
  form.cuisine.innerHTML = options([["", "不限"], ...meta.options.cuisines.map((c) => [c, c])]);
  form.budget_per_person.innerHTML = options(BUDGETS.map((b) => [b, b ? `${b.toLocaleString()} 元以內` : "不限"]));
  form.max_walk_min.innerHTML = options(WALK_LIMITS.map((w) => [w, `${w} 分鐘以內`]));
  for (const [name, value] of Object.entries(DEFAULTS)) form[name].value = value;
  updateMovies();
}

function updateMovies() {
  // 片單跟著日期變：只列出那天有上映的電影，原本選的片還在就保留
  const form = $("#plan-form");
  const day = state.meta.dates.find((d) => d.date === form.date.value);
  const current = form.movie_query.value;
  const movies = day ? day.movies : [];
  form.movie_query.innerHTML = options([["", "不限"], ...movies.map((t) => [t, t])]);
  form.movie_query.value = movies.includes(current) ? current : "";
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
  if (!req.date) return "目前沒有可以選的場次日期";
  if (toMinutes(req.home_by) <= toMinutes(req.depart_after)) return "最晚到家時間要晚於最早出門時間";
  return "";
}

// ---------- 狀態列 ----------

function renderStatus(meta, llm) {
  const llmChip = llm === undefined
    ? `<span class="dot"></span>LLM ${esc(meta.llm.model)}：檢查中`
    : llm.available && llm.model_installed
      ? `<span class="dot on"></span>LLM ${esc(llm.model)}：已連線`
      : `<span class="dot off"></span>LLM ${esc(meta.llm.model)}：${llm.available ? "未安裝模型" : "未連線"}（摘要改用範本）`;
  const jev = meta.jev.mode === "api" ? "Jev：API" : "Jev：模擬模式";
  const data = meta.data.is_demo ? "資料：示意資料" : "資料：爬蟲";
  $("#status").innerHTML = [
    `<li class="chip"><span class="dot on"></span>${jev}</li>`,
    `<li class="chip">${llmChip}</li>`,
    `<li class="chip">${data}</li>`,
  ].join("");
  const updated = meta.data.sources.map((s) => `${esc(s.source)} 更新於 ${esc(s.finished_at)}`).join("；");
  $("#foot").innerHTML = `${updated ? `${updated}。` : ""}場次、餐廳與捷運資料目前為示意資料，僅供展示流程。`;
}

// ---------- 結果 ----------

function renderSummary(result, summary, source) {
  const by = source === "llm" ? "由本機 LLM 撰寫，時間與金額都來自排程計算" : "系統自動產生";
  const text = summary || (result.plans.length
    ? `在 ${result.stats.feasible} 個接得起來的組合裡挑了 ${result.plans.length} 個方案。`
    : "找不到接得起來的行程，看看下面的原因，或放寬條件再試一次。");
  return `<section class="card summary"><p>${esc(text)}</p><p class="by">${by}</p></section>`;
}

function renderConfirmations(items) {
  if (!items.length) return "";
  const rows = items.map((item) => `
    <div class="confirm-item">
      <p>${esc(item.question)}<small>Jev 猜「${esc(item.jev_guess)}」，信心 ${Math.round(item.confidence * 100)}%</small></p>
      <div class="opts">
        ${item.options.map((opt) => `
          <button type="button" class="opt${opt === item.jev_guess ? " guess" : ""}"
            data-key="${esc(item.key)}" data-value="${esc(opt)}">${esc(opt)}</button>`).join("")}
      </div>
    </div>`);
  return `
    <section class="card confirm">
      <h2>Jev 不太確定，幫忙確認一下</h2>
      <p class="hint">信心低於門檻的判斷不會自己做決定。選完會重新排程。</p>
      ${rows.join("")}
    </section>`;
}

function renderJev(list) {
  const threshold = state.meta?.jev.threshold ?? 0.6;
  const items = list.map((a) => {
    const pct = Math.round(a.confidence * 100);
    const who = a.overridden ? "（你確認的）" : "";
    return `
      <li class="${a.confidence < threshold ? "low" : ""}">
        <span class="q">${esc(a.question)}</span>
        <span class="row"><strong>${esc(a.top)}${who}</strong><span class="bar"><i style="width:${pct}%"></i></span><span>${pct}%</span></span>
      </li>`;
  });
  return `<details class="jev"><summary>Jev 怎麼判斷的</summary><ul>${items.join("")}</ul></details>`;
}

function renderPlan(p) {
  const m = p.movie;
  const r = p.restaurant;
  const steps = p.steps.map((s) => `
    <li class="step-${esc(s.kind)}">
      <time>${esc(s.time)}</time>
      <div><strong>${esc(s.title)}</strong>${s.detail ? `<span>${esc(s.detail)}</span>` : ""}</div>
    </li>`).join("");
  const flags = p.flags.length ? `<ul class="flags">${p.flags.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : "";
  const tips = p.tips.length ? `<p class="tips">${p.tips.map(esc).join("；")}</p>` : "";
  return `
    <article class="card plan">
      <header>
        <span class="pill">${esc(p.label)}</span>
        <h3>${esc(m.start)}《${esc(m.title)}》→ ${esc(r.name)}</h3>
        <p class="sub">${esc(m.cinema)}・${esc(m.label)}・${esc(p.order_text)}</p>
      </header>
      <dl class="facts">
        <div><dt>出門</dt><dd>${esc(p.leave_home)}</dd></div>
        <div><dt>到家</dt><dd>${esc(p.arrive_home)}</dd></div>
        <div><dt>每人</dt><dd>$${p.cost_per_person.toLocaleString()}</dd></div>
        <div><dt>最緊緩衝</dt><dd>${p.tightest_buffer_min} 分</dd></div>
      </dl>
      ${flags}
      <ol class="timeline">${steps}</ol>
      ${tips}
      ${renderJev(p.jev)}
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
    result.plans.length ? `<div class="plans">${result.plans.map(renderPlan).join("")}</div>` : "",
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
  api("/api/status").then((s) => renderStatus(state.meta, s.llm)).catch(() => renderStatus(state.meta, { available: false }));

  const form = $("#plan-form");
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

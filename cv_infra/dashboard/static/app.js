// cv-infra dashboard — vanilla JS + SVG, no dependencies (the host may be offline).
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
const REFRESH_MS = 10000;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ---------------------------------------------------------------- formatting
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pad = (n) => String(n).padStart(2, "0");
function fmtTime(t) {
  if (!t) return "—";
  const d = new Date(t * 1000);
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtClock(t) {
  const d = new Date(t * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtDur(s) {
  if (s == null || !isFinite(s)) return "—";
  s = Math.round(s);
  if (s < 60) return `${s}초`;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  if (h) return `${h}시간 ${m}분`;
  return r ? `${m}분 ${r}초` : `${m}분`;
}
const fmtPct = (v, digits = 0) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`);
const fmtGB = (mib) => (mib == null ? "—" : `${(mib / 1024).toFixed(1)} GB`);
const fmtNum = (v, digits = 1) => (v == null ? "—" : Number(v).toFixed(digits));
const shortSha = (sha) => (sha ? String(sha).slice(0, 7) : "—");
const OUTCOME_KO = { pass: "통과", fail: "실패", errored: "오류", error: "오류", running: "실행 중", stale: "중단됨", sweep: "스윕" };

function outcomePill(row) {
  if (row.status === "running") return `<span class="pill running">실행 중</span>`;
  if (row.status === "stale") return `<span class="pill stale">중단됨</span>`;
  const o = row.outcome || "—";
  const cls = o === "pass" ? "pass" : o === "fail" ? "fail" : "error";
  return `<span class="pill ${cls}">${esc(OUTCOME_KO[o] || o)}</span>`;
}
function caseCounts(row) {
  return `<span class="counts"><span class="c-pass">✓ ${row.cases_pass ?? 0}</span><span class="c-fail">✗ ${row.cases_fail ?? 0}</span>${row.cases_error ? `<span class="c-error">! ${row.cases_error}</span>` : ""}</span>`;
}

// ---------------------------------------------------------------- data
async function api(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}
let windowS = 86400;
try { windowS = Number(localStorage.getItem("cv.window")) || windowS; } catch (e) { /* storage blocked */ }
windowS = Number(new URLSearchParams(location.search).get("window")) || windowS; // a shareable link wins

// ---------------------------------------------------------------- charts
function niceTicks(max, count = 4, integer = false) {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  let step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  if (integer) step = Math.max(1, Math.ceil(step));
  const ticks = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}
function timeTicks(x0, x1, count = 6) {
  const span = x1 - x0;
  const steps = [5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
  const step = steps.find((s) => span / s <= count) || 604800;
  const out = [];
  const offset = new Date().getTimezoneOffset() * 60;
  let t = Math.ceil((x0 - offset) / step) * step + offset;
  for (; t <= x1; t += step) out.push(t);
  return { ticks: out, daily: step >= 86400 };
}
function tickLabel(t, daily, span) {
  const d = new Date(t * 1000);
  if (daily || span > 3 * 86400) return `${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
  if (span < 600) return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// series: [{name, color, values: [[t, v|null]], step, area, dash}]
// gaps: a jump of more than 3x the usual sample spacing breaks the line (no data there);
// the live chart turns that off — its spacing is the browser's timer, not the data's.
function lineChart(el, { series, x0, x1, height = 180, yMax, yFmt = (v) => fmtNum(v, 0), spans = [], integer = false, gaps = true }) {
  const width = Math.max(el.clientWidth, 280);
  const m = { l: 46, r: 12, t: 8, b: 22 };
  const w = width - m.l - m.r, h = height - m.t - m.b;
  const all = series.flatMap((s) => s.values.map((p) => p[1]).filter((v) => v != null));
  const top = yMax ?? Math.max(1, ...all) * (integer ? 1 : 1.1);
  const ticks = niceTicks(top, 4, integer);
  const yTop = ticks[ticks.length - 1];
  const X = (t) => m.l + ((t - x0) / Math.max(x1 - x0, 1e-6)) * w;
  const Y = (v) => m.t + h - (v / yTop) * h;
  const { ticks: xt, daily } = timeTicks(x0, x1);
  let svg = `<svg viewBox="0 0 ${width} ${height}" height="${height}">`;
  for (const s of spans) {
    const a = Math.max(X(s.start), m.l), b = Math.min(X(s.end), m.l + w);
    if (b > a) svg += `<rect x="${a}" y="${m.t}" width="${b - a}" height="${h}" fill="var(--span)"><title>${esc(s.repo)} ${fmtTime(s.start)}–${fmtClock(s.end)}</title></rect>`;
  }
  svg += `<g class="grid">${ticks.map((v) => `<line x1="${m.l}" x2="${m.l + w}" y1="${Y(v)}" y2="${Y(v)}"/>`).join("")}</g>`;
  svg += `<g class="axis">${ticks.map((v) => `<text x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end">${esc(yFmt(v))}</text>`).join("")}`;
  svg += xt.map((t) => `<text x="${X(t)}" y="${height - 6}" text-anchor="middle">${tickLabel(t, daily, x1 - x0)}</text>`).join("") + `</g>`;
  for (const s of series) {
    const gap = gaps ? medianGap(s.values) * 3 : Infinity;
    let d = "", prev = null, area = "", segStart = null;
    for (const [t, v] of s.values) {
      if (v == null || (prev && t - prev[0] > gap)) {
        if (s.area && segStart != null && prev) area += `L${X(prev[0])},${Y(0)}L${X(segStart)},${Y(0)}Z`;
        if (v == null) { prev = null; segStart = null; continue; }
        prev = null; segStart = null;
      }
      const x = X(t), y = Y(Math.min(v, yTop));
      if (!prev) { d += `M${x},${y}`; segStart = t; area += `M${x},${y}`; }
      else if (s.step) { d += `L${x},${Y(Math.min(prev[1], yTop))}L${x},${y}`; area += `L${x},${Y(Math.min(prev[1], yTop))}L${x},${y}`; }
      else { d += `L${x},${y}`; area += `L${x},${y}`; }
      prev = [t, v];
    }
    if (s.area && segStart != null && prev) area += `L${X(prev[0])},${Y(0)}L${X(segStart)},${Y(0)}Z`;
    if (s.area) svg += `<path d="${area}" fill="${s.color}" opacity="0.12"/>`;
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.8" ${s.dash ? 'stroke-dasharray="4 3"' : ""}/>`;
  }
  svg += `<line class="cursor" x1="0" x2="0" y1="${m.t}" y2="${m.t + h}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/>`;
  svg += `<rect class="hit" x="${m.l}" y="${m.t}" width="${w}" height="${h}" fill="transparent"/></svg>`;
  const legend = `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>`;
  el.innerHTML = `${legend}<div class="chart">${svg}<div class="tip"></div></div>`;
  const chart = $(".chart", el), tip = $(".tip", el), cursor = $(".cursor", el), hit = $(".hit", el);
  hit.addEventListener("mousemove", (ev) => {
    const box = hit.getBoundingClientRect();
    const t = x0 + ((ev.clientX - box.left) / box.width) * (x1 - x0);
    const lines = series.map((s) => {
      const p = nearest(s.values, t);
      return p ? `<div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${s.color};margin-right:6px"></i>${esc(s.name)}: <b>${esc(yFmt(p[1]))}</b></div>` : "";
    }).join("");
    cursor.setAttribute("x1", X(t)); cursor.setAttribute("x2", X(t)); cursor.setAttribute("visibility", "visible");
    tip.innerHTML = `<div style="color:var(--muted)">${fmtTime(t)}:${pad(new Date(t * 1000).getSeconds())}</div>${lines}`;
    tip.style.display = "block";
    const cb = chart.getBoundingClientRect();
    const left = ev.clientX - cb.left + 14;
    tip.style.left = `${Math.min(left, cb.width - tip.offsetWidth - 4)}px`;
    tip.style.top = `${ev.clientY - cb.top + 10}px`;
  });
  hit.addEventListener("mouseleave", () => { tip.style.display = "none"; cursor.setAttribute("visibility", "hidden"); });
}
function medianGap(values) {
  const gaps = [];
  for (let i = 1; i < values.length; i++) gaps.push(values[i][0] - values[i - 1][0]);
  if (!gaps.length) return Infinity;
  gaps.sort((a, b) => a - b);
  return Math.max(gaps[Math.floor(gaps.length / 2)], 1);
}
function nearest(values, t) {
  let best = null, dist = Infinity;
  for (const p of values) {
    if (p[1] == null) continue;
    const d = Math.abs(p[0] - t);
    if (d < dist) { dist = d; best = p; }
  }
  return best;
}

function stackedBars(el, { days, height = 180 }) {
  const width = Math.max(el.clientWidth, 280);
  const m = { l: 34, r: 12, t: 8, b: 22 };
  const w = width - m.l - m.r, h = height - m.t - m.b;
  const totals = days.map((d) => d.pass + d.fail + d.error);
  const ticks = niceTicks(Math.max(1, ...totals), 4, true);
  const top = ticks[ticks.length - 1];
  const bw = Math.min(w / Math.max(days.length, 1), 64);
  const Y = (v) => m.t + h - (v / top) * h;
  const colors = { pass: css("--pass"), fail: css("--fail"), error: css("--error") };
  let svg = `<svg viewBox="0 0 ${width} ${height}" height="${height}">`;
  svg += `<g class="grid">${ticks.map((v) => `<line x1="${m.l}" x2="${m.l + w}" y1="${Y(v)}" y2="${Y(v)}"/>`).join("")}</g>`;
  svg += `<g class="axis">${ticks.map((v) => `<text x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end">${v}</text>`).join("")}`;
  const every = Math.ceil(days.length / 10);
  days.forEach((d, i) => {
    if (i % every === 0) {
      const dt = new Date(d.day * 1000);
      svg += `<text x="${m.l + bw * (i + 0.5)}" y="${height - 6}" text-anchor="middle">${pad(dt.getMonth() + 1)}/${pad(dt.getDate())}</text>`;
    }
  });
  svg += `</g>`;
  days.forEach((d, i) => {
    let acc = 0;
    for (const key of ["pass", "fail", "error"]) {
      if (!d[key]) continue;
      const y0 = Y(acc), y1 = Y(acc + d[key]);
      svg += `<rect x="${m.l + bw * i + bw * 0.15}" y="${y1}" width="${bw * 0.7}" height="${y0 - y1}" fill="${colors[key]}" rx="2"><title>${fmtTime(d.day).slice(0, 5)} ${OUTCOME_KO[key]} ${d[key]}건 · 실행 ${fmtDur(d.busy_s)}</title></rect>`;
      acc += d[key];
    }
  });
  svg += `</svg>`;
  el.innerHTML = `<div class="legend"><span><i style="background:${colors.pass}"></i>통과</span><span><i style="background:${colors.fail}"></i>실패</span><span><i style="background:${colors.error}"></i>오류</span></div><div class="chart">${svg}</div>`;
}

// Case runs as bars on the run's time axis, packed into as few lanes as possible: the
// number of lanes in use at any moment IS the concurrency at that moment.
function timeline(el, { cases, x0, x1 }) {
  const timed = cases.filter((c) => c.started_at && c.ended_at).sort((a, b) => a.started_at - b.started_at);
  if (!timed.length) { el.innerHTML = `<div class="empty">케이스 시각 기록이 없습니다(가져온 기록).</div>`; return; }
  const lanes = [];
  for (const c of timed) {
    let lane = lanes.findIndex((end) => end <= c.started_at + 0.5);
    if (lane < 0) { lane = lanes.length; lanes.push(0); }
    lanes[lane] = c.ended_at;
    c._lane = lane;
  }
  const rowH = 18, width = Math.max(el.clientWidth, 280);
  const m = { l: 46, r: 12, t: 6, b: 22 };
  const w = width - m.l - m.r, height = m.t + lanes.length * rowH + m.b;
  const X = (t) => m.l + ((t - x0) / Math.max(x1 - x0, 1e-6)) * w;
  const colors = { pass: css("--pass"), fail: css("--fail"), error: css("--error"), ran: css("--run") };
  const { ticks, daily } = timeTicks(x0, x1);
  let svg = `<svg viewBox="0 0 ${width} ${height}" height="${height}"><g class="grid">${ticks.map((t) => `<line x1="${X(t)}" x2="${X(t)}" y1="${m.t}" y2="${height - m.b}"/>`).join("")}</g>`;
  svg += `<g class="axis">${lanes.map((_, i) => `<text x="${m.l - 6}" y="${m.t + i * rowH + 13}" text-anchor="end">${i + 1}</text>`).join("")}`;
  svg += ticks.map((t) => `<text x="${X(t)}" y="${height - 6}" text-anchor="middle">${tickLabel(t, daily, x1 - x0)}</text>`).join("") + `</g>`;
  for (const c of timed) {
    const r = caseResult(c);
    const axes = Object.entries(c.axes || {}).map(([k, v]) => `${k}=${v}`).join(" ");
    svg += `<rect x="${X(c.started_at)}" y="${m.t + c._lane * rowH + 2}" width="${Math.max(X(c.ended_at) - X(c.started_at), 2)}" height="${rowH - 5}" rx="3" fill="${colors[r]}" opacity="0.85"><title>#${c.case_index} r${c.repeat} · ${OUTCOME_KO[r] || r} · ${fmtDur(c.ended_at - c.started_at)}${c.gpu_retries ? " · GPU 재시도" : ""}\n${esc(axes)}</title></rect>`;
  }
  svg += `</svg>`;
  el.innerHTML = `<div class="legend"><span>세로축 = 동시 실행 슬롯(그 순간 몇 개가 함께 돌았나)</span><span><i style="background:${colors.pass}"></i>통과</span><span><i style="background:${colors.fail}"></i>실패</span><span><i style="background:${colors.error}"></i>오류</span></div><div class="chart">${svg}</div>`;
}
function caseResult(c) {
  if (c.lane === "error") return "error";
  const checks = Object.values(c.checks || {});
  if (!checks.length) return "ran";
  return checks.every(Boolean) ? "pass" : "fail";
}

// ---------------------------------------------------------------- shared bits
const kpi = (label, value, sub = "") => `<div class="panel kpi"><div class="label">${label}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
function gauge(label, frac, text, color, of = "") {
  const pct = frac == null ? 0 : Math.max(0, Math.min(1, frac)) * 100;
  return `<div class="gauge"><div class="label">${label}${of ? ` <span class="of">${of}</span>` : ""}</div><div class="num">${text}</div><div class="bar"><span style="width:${pct}%;background:${color}"></span></div></div>`;
}
function hostGauges(host) {
  const gpuMem = host.gpu_total_mib ? host.gpu_used_mib / host.gpu_total_mib : null;
  const ramUsed = host.ram_total_mib != null && host.ram_available_mib != null ? host.ram_total_mib - host.ram_available_mib : null;
  return `<div class="gauges">
    ${gauge("GPU 사용률", host.gpu_util_pct == null ? null : host.gpu_util_pct / 100, host.gpu_util_pct == null ? "—" : `${fmtNum(host.gpu_util_pct, 0)}%`, css("--gpu"))}
    ${gauge("GPU 메모리", gpuMem, fmtGB(host.gpu_used_mib), css("--mem"), `/ ${fmtGB(host.gpu_total_mib)}`)}
    ${gauge("RAM", ramUsed == null ? null : ramUsed / host.ram_total_mib, fmtGB(ramUsed), css("--ram"), `/ ${fmtGB(host.ram_total_mib)}`)}
    ${gauge("CPU 부하(1분)", null, fmtNum(host.load1, 2), css("--load"))}
  </div>`;
}
function seriesOf(points, key, scale = 1) { return points.map((p) => [p.t, p[key] == null ? null : p[key] * scale]); }
// Occupancy = the share of the WINDOW during which at least one verify run was in
// flight, from the runs' own start/end (the union of their spans). Samples exist only
// while runs run, so a share of sampled time would always read 100%.
function busyShare(spans, since, until) {
  const cut = spans.map((s) => [Math.max(s.start, since), Math.min(s.end, until)]).filter(([a, b]) => b > a).sort((x, y) => x[0] - y[0]);
  let total = 0, end = -Infinity;
  for (const [a, b] of cut) {
    if (b <= end) continue;
    total += b - Math.max(a, end);
    end = b;
  }
  return until > since ? total / (until - since) : null;
}
function resourceStats(points, since, until, spans = []) {
  const busy = points.filter((p) => p.running > 0);
  const util = points.filter((p) => p.gpu_util_pct != null);
  return {
    occupancy: busyShare(spans, since, until),
    gpuMean: util.length ? util.reduce((a, p) => a + p.gpu_util_pct, 0) / util.length : null,
    gpuBusyMean: busy.length ? busy.reduce((a, p) => a + (p.gpu_util_pct || 0), 0) / busy.length : null,
    runningMean: busy.length ? busy.reduce((a, p) => a + p.running, 0) / busy.length : null,
    runningPeak: Math.max(0, ...points.map((p) => p.running || 0)),
    gpuMemPeak: Math.max(0, ...points.map((p) => p.gpu_used_mib || 0)),
    span: until - since,
  };
}

// ---------------------------------------------------------------- pages
const pages = {};

pages.overview = {
  mount() {
    app.innerHTML = `
      <div id="kpis" class="grid kpis"></div><div id="nohist"></div>
      <div class="grid two">
        <div class="panel"><h2>지금 호스트</h2><div id="host"></div><h3 style="margin-top:16px">실행 중인 작업</h3><div id="active"></div></div>
        <div class="panel"><h2>동시 실행 케이스 · 스케줄러 레벨</h2><div id="mini-run"></div></div>
        <div class="panel"><h2>일별 작업 결과</h2><div id="daily"></div></div>
        <div class="panel"><h2>GPU 사용률</h2><div id="mini-gpu"></div></div>
      </div>
      <div class="panel" style="margin-top:16px"><h2>저장소별 요청</h2><div id="repos" class="scroll"></div></div>`;
  },
  async update() {
    const [o, s] = await Promise.all([api(`/api/overview?window=${windowS}`), api(`/api/series?window=${windowS}&buckets=240`)]);
    const k = o.kpi, st = resourceStats(s.points, s.since, s.until, s.runs);
    $("#kpis").innerHTML = [
      kpi("검증 요청", k.runs, `완료 ${k.runs_done} · 실행 중 ${o.active.length}${k.runs_stale ? ` · 중단 ${k.runs_stale}` : ""}`),
      kpi("작업 통과", k.runs_done ? `${k.runs_passed}/${k.runs_done}` : "—", k.runs_done ? fmtPct(k.runs_passed / k.runs_done) : ""),
      kpi("케이스 통과율", fmtPct(k.case_pass_rate, 1), `✓ ${k.cases.pass} · ✗ ${k.cases.fail} · ! ${k.cases.error}`),
      kpi("평균 소요", fmtDur(k.mean_duration_s), `실행 시간 합 ${fmtDur(k.busy_s)}`),
      kpi("가동률", fmtPct(st.occupancy, 1), "기간 중 검증 작업이 돈 시간"),
      kpi("최대 병렬", Math.max(k.peak_concurrency || 0, st.runningPeak) || "—", st.runningMean ? `가동 중 평균 ${fmtNum(st.runningMean, 1)} (모든 작업 합)` : ""),
    ].join("");
    $("#host").innerHTML = hostGauges(o.host);
    $("#nohist").innerHTML = o.history.present ? "" : `<div class="panel note" style="margin-bottom:16px">아직 실행 기록이 없습니다 — cv-infra verify가 처음 돌면 <code>${esc(o.history.path)}</code>에 쌓입니다.</div>`;
    $("#active").innerHTML = o.active.length ? runsTable(o.active, { compact: true }) : `<div class="note">없음</div>`;
    lineChart($("#mini-run"), { integer: true, x0: s.since, x1: s.until, spans: s.runs, height: 170, series: [
      { name: "동시 실행", color: css("--run"), values: seriesOf(s.points, "running"), step: true, area: true },
      { name: "레벨", color: css("--level"), values: seriesOf(s.points, "level"), step: true, dash: true },
    ] });
    lineChart($("#mini-gpu"), { x0: s.since, x1: s.until, spans: s.runs, height: 170, yMax: 100, yFmt: (v) => `${fmtNum(v, 0)}%`, series: [
      { name: "GPU 사용률", color: css("--gpu"), values: seriesOf(s.points, "gpu_util_pct"), area: true },
    ] });
    stackedBars($("#daily"), { days: o.daily, height: 170 });
    $("#repos").innerHTML = o.by_repo.length ? `<table><thead><tr><th>저장소</th><th class="num">작업</th><th class="num">작업 통과</th><th>케이스</th><th class="num">평균 소요</th><th>마지막 요청</th></tr></thead><tbody>${o.by_repo.map((r) => `
      <tr class="click" data-repo="${esc(r.repo)}"><td>${esc(r.repo)}</td><td class="num">${r.runs}</td><td class="num">${r.passed}/${r.runs}</td><td>${caseCounts(r)}</td><td class="num">${fmtDur(r.mean_duration_s)}</td><td>${fmtTime(r.last_started_at)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">이 기간에 끝난 작업이 없습니다.</div>`;
    app.querySelectorAll("tr[data-repo]").forEach((tr) => tr.addEventListener("click", () => { runsState.repo = tr.dataset.repo; location.hash = "#/runs"; }));
    bindRunRows();
  },
};

// The live view keeps what it has seen in THIS page only: nothing on the host stores it.
const LIVE_MS = 3000, LIVE_KEEP = 400;
const liveBuf = [];
async function pollLive() {
  const live = await api("/api/live");
  liveBuf.push({ t: live.now, ...live.host });
  if (liveBuf.length > LIVE_KEEP) liveBuf.shift();
  return live;
}
function drawLive() {
  const el = $("#c-live");
  if (!el || liveBuf.length < 2) return;
  const pct = (a, b) => liveBuf.map((p) => [p.t, p[a] != null && p[b] ? (100 * p[a]) / p[b] : null]);
  lineChart(el, { x0: liveBuf[0].t, x1: liveBuf[liveBuf.length - 1].t, height: 190, yMax: 100, gaps: false, yFmt: (v) => `${fmtNum(v, 0)}%`, series: [
    { name: "GPU 사용률", color: css("--gpu"), values: liveBuf.map((p) => [p.t, p.gpu_util_pct]) },
    { name: "GPU 메모리", color: css("--mem"), values: pct("gpu_used_mib", "gpu_total_mib") },
    { name: "RAM", color: css("--ram"), values: liveBuf.map((p) => [p.t, p.ram_total_mib && p.ram_available_mib != null ? 100 * (1 - p.ram_available_mib / p.ram_total_mib) : null]) },
  ] });
}

pages.resources = {
  mount() {
    app.innerHTML = `
      <div class="panel" style="margin-bottom:16px"><h2>실시간 <small style="color:var(--muted);font-weight:400">— ${LIVE_MS / 1000}초마다 호스트를 직접 읽어, 이 화면을 연 뒤의 흐름만 보여줍니다(어디에도 저장하지 않음)</small></h2>
        <div class="grid two"><div id="host"></div><div id="c-live"><div class="note">수집 중…</div></div></div></div>
      <div id="stats" class="grid kpis"></div>
      <div class="grid two">
        <div class="panel"><h2>동시 실행 케이스 · 스케줄러 레벨</h2><div id="c-run"></div><div class="note">음영 = 검증 작업이 돈 구간. 동시 실행은 같은 시각에 돈 모든 작업의 합.</div></div>
        <div class="panel"><h2>GPU 사용률</h2><div id="c-gpu"></div></div>
        <div class="panel"><h2>GPU 메모리</h2><div id="c-gmem"></div></div>
        <div class="panel"><h2>RAM 사용량</h2><div id="c-ram"></div></div>
        <div class="panel"><h2>CPU 부하(1분 평균)</h2><div id="c-load"></div><div class="note">아래 그래프들은 검증 작업이 돌던 동안 cv-infra verify가 남긴 기록입니다(작업 사이 구간은 비어 있음).</div></div>
      </div>`;
    const tick = async () => {
      try { const live = await pollLive(); $("#host").innerHTML = hostGauges(live.host); drawLive(); } catch (e) { /* the main refresh reports connection loss */ }
    };
    tick();
    this.liveTimer = setInterval(tick, LIVE_MS);
  },
  unmount() { clearInterval(this.liveTimer); },
  async update() {
    const s = await api(`/api/series?window=${windowS}`);
    const st = resourceStats(s.points, s.since, s.until, s.runs);
    $("#stats").innerHTML = [
      kpi("가동률", fmtPct(st.occupancy, 1), "기간 중 검증 작업이 돈 시간"),
      kpi("평균 GPU 사용률", st.gpuMean == null ? "—" : `${fmtNum(st.gpuMean, 0)}%`, st.gpuBusyMean == null ? "" : `가동 중 ${fmtNum(st.gpuBusyMean, 0)}%`),
      kpi("최대 동시 실행", st.runningPeak || "—", st.runningMean ? `가동 중 평균 ${fmtNum(st.runningMean, 1)}` : ""),
      kpi("GPU 메모리 피크", fmtGB(st.gpuMemPeak || null)),
      kpi("작업 구간", s.runs.length, "이 기간에 겹친 검증 작업"),
    ].join("");
    const base = { x0: s.since, x1: s.until, spans: s.runs, height: 210 };
    lineChart($("#c-run"), { integer: true, ...base, series: [
      { name: "동시 실행", color: css("--run"), values: seriesOf(s.points, "running"), step: true, area: true },
      { name: "레벨", color: css("--level"), values: seriesOf(s.points, "level"), step: true, dash: true },
    ] });
    lineChart($("#c-gpu"), { ...base, yMax: 100, yFmt: (v) => `${fmtNum(v, 0)}%`, series: [{ name: "GPU 사용률", color: css("--gpu"), values: seriesOf(s.points, "gpu_util_pct"), area: true }] });
    lineChart($("#c-gmem"), { ...base, yFmt: (v) => `${fmtNum(v, 0)} GB`, series: [
      { name: "사용", color: css("--mem"), values: seriesOf(s.points, "gpu_used_mib", 1 / 1024), area: true },
      { name: "전체", color: css("--muted"), values: seriesOf(s.points, "gpu_total_mib", 1 / 1024), dash: true },
    ] });
    lineChart($("#c-ram"), { ...base, yFmt: (v) => `${fmtNum(v, 0)} GB`, series: [
      { name: "사용", color: css("--ram"), values: seriesOf(s.points, "ram_used_mib", 1 / 1024), area: true },
      { name: "전체", color: css("--muted"), values: seriesOf(s.points, "ram_total_mib", 1 / 1024), dash: true },
    ] });
    lineChart($("#c-load"), { ...base, yFmt: (v) => fmtNum(v, 1), series: [{ name: "load1", color: css("--load"), values: seriesOf(s.points, "load1"), area: true }] });
    drawLive();
  },
};

const runsState = { repo: "", outcome: "", q: "", sort: "started_at", dir: -1 };
function runsTable(rows, { compact = false } = {}) {
  return `<table><thead><tr>
    <th>시작</th><th>저장소</th><th>커밋</th><th>이벤트</th>${compact ? "" : "<th>모드</th>"}<th>결과</th><th>케이스</th><th class="num">소요</th>${compact ? "" : `<th class="num">최대 병렬</th><th class="num">회귀</th>`}<th></th>
  </tr></thead><tbody>${rows.map((r) => `
    <tr class="click" data-run="${esc(r.run_id)}">
      <td>${fmtTime(r.started_at)}</td><td>${esc(r.repo)}</td><td class="mono">${esc(shortSha(r.sha))}</td><td>${esc(r.event || "—")}</td>
      ${compact ? "" : `<td>${esc(r.mode || "—")}</td>`}<td>${outcomePill(r)}</td>
      <td>${r.status === "running" ? `${r.cases_planned ?? "?"}개 계획` : caseCounts(r)}</td>
      <td class="num">${fmtDur((r.ended_at || Date.now() / 1000) - r.started_at)}</td>
      ${compact ? "" : `<td class="num">${r.peak_concurrency ?? "—"}</td><td class="num">${r.regressions ?? "—"}</td>`}
      <td>${r.gh_run_url ? `<a href="${esc(r.gh_run_url)}" target="_blank" rel="noopener">GitHub ↗</a>` : ""}</td>
    </tr>`).join("")}</tbody></table>`;
}
function bindRunRows() {
  app.querySelectorAll("tr[data-run]").forEach((tr) => tr.addEventListener("click", (ev) => {
    if (ev.target.closest("a")) return;
    location.hash = `#/runs/${tr.dataset.run}`;
  }));
}

pages.runs = {
  rows: [],
  mount() {
    app.innerHTML = `
      <div class="panel">
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
          <h2 style="margin:0">작업(검증 요청)</h2><div class="spacer"></div>
          <select id="f-repo"><option value="">모든 저장소</option></select>
          <select id="f-outcome"><option value="">모든 결과</option><option value="pass">통과</option><option value="fail">실패</option><option value="errored">오류</option><option value="running">실행 중</option><option value="stale">중단됨</option></select>
          <input id="f-q" type="search" placeholder="커밋·브랜치·이벤트 검색">
        </div>
        <div id="runs" class="scroll"></div>
        <div id="runs-note" class="note"></div>
      </div>`;
    $("#f-q").value = runsState.q;
    $("#f-outcome").value = runsState.outcome;
    for (const [id, key] of [["#f-repo", "repo"], ["#f-outcome", "outcome"], ["#f-q", "q"]]) {
      $(id).addEventListener("input", (ev) => { runsState[key] = ev.target.value; this.draw(); });
    }
  },
  async update() {
    const data = await api(`/api/runs?window=${windowS}`);
    this.rows = data.runs;
    const repos = [...new Set(this.rows.map((r) => r.repo))].sort();
    const sel = $("#f-repo");
    sel.innerHTML = `<option value="">모든 저장소</option>${repos.map((r) => `<option ${r === runsState.repo ? "selected" : ""}>${esc(r)}</option>`).join("")}`;
    this.draw();
  },
  draw() {
    const q = runsState.q.toLowerCase();
    let rows = this.rows.filter((r) =>
      (!runsState.repo || r.repo === runsState.repo) &&
      (!runsState.outcome || (["running", "stale"].includes(runsState.outcome) ? r.status === runsState.outcome : r.status === "done" && r.outcome === runsState.outcome)) &&
      (!q || [r.sha, r.ref, r.event, r.actor, r.repo].some((v) => String(v || "").toLowerCase().includes(q))));
    rows = rows.sort((a, b) => {
      const va = a[runsState.sort] ?? -Infinity, vb = b[runsState.sort] ?? -Infinity;
      return (va > vb ? 1 : va < vb ? -1 : 0) * runsState.dir;
    });
    $("#runs").innerHTML = rows.length ? runsTable(rows) : `<div class="empty">조건에 맞는 작업이 없습니다.</div>`;
    $("#runs-note").textContent = `${rows.length}건 / 이 기간 ${this.rows.length}건 · 행을 누르면 상세`;
    const keys = ["started_at", "repo", "sha", "event", "mode", "outcome", "cases_pass", null, "peak_concurrency", "regressions"];
    app.querySelectorAll("#runs th").forEach((th, i) => {
      if (!keys[i]) return;
      th.classList.add("sort");
      if (runsState.sort === keys[i]) th.textContent += runsState.dir > 0 ? " ▲" : " ▼";
      th.addEventListener("click", () => { runsState.dir = runsState.sort === keys[i] ? -runsState.dir : -1; runsState.sort = keys[i]; this.draw(); });
    });
    bindRunRows();
  },
};

pages.run = {
  mount(id) {
    this.id = id;
    app.innerHTML = `<a class="back" href="#/runs">← 작업 목록</a><div id="detail"><div class="empty">불러오는 중…</div></div>`;
  },
  async update() {
    const [r, s] = await Promise.all([api(`/api/runs/${this.id}`), api(`/api/series?run_id=${this.id}&buckets=400`)]);
    const end = r.ended_at || Date.now() / 1000;
    const cases = r.cases.length ? r.cases : casesFromReport(r.report);
    const st = resourceStats(s.points, s.since, s.until);
    $("#detail").innerHTML = `
      <div class="panel" style="margin-bottom:16px">
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <h2 style="margin:0">${esc(r.repo)} <span class="mono" style="color:var(--muted)">${esc(shortSha(r.sha))}</span></h2>${outcomePill(r)}
          <div class="spacer"></div>${r.gh_run_url ? `<a href="${esc(r.gh_run_url)}" target="_blank" rel="noopener">GitHub 실행 ↗</a>` : ""}
        </div>
      </div>
      <div class="grid kpis">
        ${kpi("케이스", `${r.cases_run ?? "—"}/${r.cases_planned ?? "—"}`, caseCounts(r))}
        ${kpi("소요", fmtDur(end - r.started_at), `${fmtTime(r.started_at)} 시작`)}
        ${kpi("최대 병렬", r.peak_concurrency ?? "—", st.runningMean ? `평균 ${fmtNum(st.runningMean, 1)}` : "")}
        ${kpi("실패한 체크", r.checks_failed ?? "—", `회귀 ${r.regressions ?? "—"}`)}
        ${kpi("GPU 평균 사용률", st.gpuMean == null ? "—" : `${fmtNum(st.gpuMean, 0)}%`, `GPU 메모리 피크 ${fmtGB(st.gpuMemPeak || null)}`)}
      </div>
      <div class="grid two">
        <div class="panel"><h2>요청</h2><dl class="meta">
          <dt>저장소</dt><dd>${esc(r.repo)}</dd><dt>커밋</dt><dd class="mono">${esc(r.sha || "—")}</dd>
          <dt>ref</dt><dd>${esc(r.ref || "—")}</dd><dt>이벤트</dt><dd>${esc(r.event || "—")}${r.actor ? ` · ${esc(r.actor)}` : ""}</dd>
          <dt>호스트</dt><dd>${esc(r.host || "—")}</dd><dt>시작 · 종료</dt><dd>${fmtTime(r.started_at)} → ${r.ended_at ? fmtTime(r.ended_at) : "실행 중"}</dd>
        </dl></div>
        <div class="panel"><h2>입력</h2><dl class="meta">
          <dt>sim_script</dt><dd class="mono">${esc(r.sim_script || "—")}</dd><dt>입력 공간</dt><dd class="mono">${esc(r.input_space || "—")} · k=${esc(r.pict_k ?? "—")}</dd>
          <dt>이미지</dt><dd class="mono">${esc(r.sim_image || "—")}</dd><dt>반복 · 병렬</dt><dd>repeats ${esc(r.repeats ?? "—")} · concurrency ${esc(r.concurrency ?? "—")}</dd>
          <dt>모드 · 예산</dt><dd>${esc(r.mode || "—")} · ${r.budget_s ? fmtDur(r.budget_s) : "없음"}</dd>
        </dl></div>
      </div>
      <div class="panel" style="margin-top:16px"><h2>케이스 타임라인</h2><div id="tl"></div></div>
      <div class="grid two" style="margin-top:16px">
        <div class="panel"><h2>동시 실행 · 레벨</h2><div id="r-run"></div></div>
        <div class="panel"><h2>GPU 사용률 · 메모리</h2><div id="r-gpu"></div></div>
      </div>
      <div class="panel" style="margin-top:16px"><h2>케이스 (${cases.length})</h2><div class="scroll">${casesTable(cases)}</div></div>`;
    timeline($("#tl"), { cases, x0: r.started_at, x1: end });
    if (s.points.length) {
      lineChart($("#r-run"), { integer: true, x0: s.since, x1: s.until, height: 170, series: [
        { name: "동시 실행", color: css("--run"), values: seriesOf(s.points, "running"), step: true, area: true },
        { name: "레벨", color: css("--level"), values: seriesOf(s.points, "level"), step: true, dash: true },
      ] });
      lineChart($("#r-gpu"), { x0: s.since, x1: s.until, height: 170, yMax: 100, yFmt: (v) => fmtNum(v, 0), series: [
        { name: "GPU 사용률 %", color: css("--gpu"), values: seriesOf(s.points, "gpu_util_pct"), area: true },
        { name: "GPU 메모리 %", color: css("--mem"), values: s.points.map((p) => [p.t, p.gpu_total_mib ? (100 * p.gpu_used_mib) / p.gpu_total_mib : null]) },
      ] });
    } else {
      $("#r-run").innerHTML = $("#r-gpu").innerHTML = `<div class="empty">이 작업의 자원 샘플이 없습니다(가져온 기록).</div>`;
    }
  },
};
function casesFromReport(report) {
  if (!report) return [];
  return report.matrix.flatMap((row, i) => row.runs.map((run) => ({
    case_index: i, repeat: run.repeat, axes: row.axes, lane: run.error ? "error" : "ok", error: run.error,
    wall_s: run.wall_s, gpu_retries: run.gpu_retries || 0,
    checks: Object.fromEntries(Object.entries((run.verdict || {})).filter(([, v]) => typeof v === "boolean")),
    metrics: Object.fromEntries(Object.entries((run.verdict || {})).filter(([, v]) => typeof v === "number")),
    notes: Object.fromEntries(Object.entries((run.verdict || {})).filter(([, v]) => typeof v === "string")),
  })));
}
function casesTable(cases) {
  if (!cases.length) return `<div class="empty">케이스 기록이 없습니다.</div>`;
  return `<table><thead><tr><th class="num">#</th><th class="num">반복</th><th>축</th><th>결과</th><th>체크</th><th>지표</th><th class="num">시간</th><th>메모 · 오류</th></tr></thead><tbody>${cases.map((c) => {
    const r = caseResult(c);
    const checks = Object.entries(c.checks || {}).map(([k, v]) => `<span class="${v ? "c-pass" : "c-fail"}">${v ? "✓" : "✗"} ${esc(k)}</span>`).join(" ");
    const metrics = Object.entries(c.metrics || {}).slice(0, 4).map(([k, v]) => `${esc(k)}=${esc(typeof v === "number" ? +v.toFixed(3) : v)}`).join(" · ");
    const note = c.error ? `<span class="c-error">${esc(c.error)}</span>` : esc(Object.values(c.notes || {}).join(" "));
    return `<tr><td class="num">${c.case_index}</td><td class="num">${c.repeat}${c.gpu_retries ? " ↻" : ""}</td>
      <td class="wrap mono">${Object.entries(c.axes || {}).map(([k, v]) => `${esc(k)}=${esc(v)}`).join(" ")}</td>
      <td><span class="pill ${r === "ran" ? "" : r}">${esc(OUTCOME_KO[r] || "실행")}</span></td>
      <td class="wrap">${checks || "—"}</td><td class="wrap mono">${metrics || "—"}</td>
      <td class="num">${fmtDur(c.wall_s)}</td><td class="wrap">${note}</td></tr>`;
  }).join("")}</tbody></table>`;
}

// ---------------------------------------------------------------- router + refresh
let current = null, timer = null;
function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  let page = parts[0] || "overview", arg = parts[1];
  if (page === "runs" && arg) page = "run";
  if (!pages[page]) page = "overview";
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("on", a.dataset.page === (page === "run" ? "runs" : page)));
  if (current && current.unmount) current.unmount();
  current = pages[page];
  current.mount(arg);
  refresh();
}
async function refresh() {
  clearTimeout(timer);
  const page = current;
  try {
    await page.update();
    $("#live").className = "dot";
    $("#updated").textContent = fmtClock(Date.now() / 1000);
  } catch (err) {
    $("#live").className = "dot off";
    $("#updated").textContent = `연결 실패 (${err.message})`;
  }
  if (page === current) timer = setTimeout(refresh, REFRESH_MS);
}
$("#window").value = String(windowS);
$("#window").addEventListener("change", (ev) => {
  windowS = Number(ev.target.value);
  try { localStorage.setItem("cv.window", String(windowS)); } catch (e) { /* storage blocked */ }
  refresh();
});
window.addEventListener("hashchange", route);
let resizeTimer = null;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(refresh, 200); });
route();

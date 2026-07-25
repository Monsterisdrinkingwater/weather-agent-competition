/* 行山对账 — 前端逻辑（原生 JS，无框架） */
"use strict";

var API = "";               // 同源部署
var state = {
  routes: [],               // 全部线路
  route: null,              // 当前选中线路
  gearItems: null,          // 已解析装备
  plan: null,               // 当前计划
  report: null,             // 最近一次对账结果
  meta: { weather_source: "demo", llm_enabled: false, web_search_enabled: false },
};

var KIND_LABEL = { start: "起点", pass: "垭口", camp: "营地", peak: "山顶", water: "水源/横渡", finish: "终点", aid: "补给站" };
var CAT_LABEL = { sleep: "睡眠", shelter: "帐篷", rain: "防雨", warm: "保暖", footwear: "鞋袜", other: "其他" };
var CONF_LABEL = { high: "联网核实", medium: "AI 估计", low: "内置知识库" };
var SEV_ICON = { danger: "⛔", warning: "⚠️", info: "ℹ️" };

// ── 工具 ──────────────────────────────────────────────

function $(id) { return document.getElementById(id); }

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

function toast(msg, ms) {
  var t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(function () { t.classList.add("hidden"); }, ms || 2600);
}

function api(path, opts) {
  return fetch(API + path, opts).then(function (res) {
    if (!res.ok) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        throw new Error(body.detail || ("请求失败 " + res.status));
      });
    }
    return res.json();
  });
}

function postJSON(path, data) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

function fmtDate(iso) {
  if (!iso) return "";
  var d = new Date(iso);
  return (d.getMonth() + 1) + "/" + d.getDate() + " " +
    String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function dayLabel(dateStr) {
  return dateStr ? dateStr.slice(5).replace("-", "/") : "";
}

// ── 视图切换 ──────────────────────────────────────────

function showView(name) {
  ["routes", "plan", "report", "push"].forEach(function (v) {
    $("view-" + v).classList.toggle("hidden", v !== name);
  });
  window.scrollTo(0, 0);
}

// ── 初始化 ────────────────────────────────────────────

function init() {
  api("/api/meta").then(function (m) {
    state.meta = m;
    var badge = $("sourceBadge");
    if (m.weather_source === "demo") {
      badge.textContent = "演示模式 · 模拟气象数据";
      badge.classList.remove("live");
    } else {
      badge.textContent = "天机气象 · 实时数据";
      badge.classList.add("live");
    }
    $("demoScenarios").classList.toggle("hidden", m.weather_source !== "demo");
  }).catch(function () { $("sourceBadge").textContent = "离线"; });

  loadRoutes();

  // 出发日期默认 +3 天
  var d = new Date(Date.now() + 3 * 86400000);
  $("departDate").value = d.toISOString().slice(0, 10);
}

function loadRoutes() {
  api("/api/routes").then(function (data) {
    state.routes = data.routes || [];
    renderRoutes("all");
  }).catch(function (e) { toast("线路加载失败：" + e.message); });
}

// ── 视图 1：线路 ──────────────────────────────────────

function _hashSeed(str) {
  var h = 2166136261;
  for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

function _rng(seed) {
  return function () {
    seed = seed + 0x6D2B79F5 | 0;
    var t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}

function _havKm(a, b) {
  var R = 6371, d2r = Math.PI / 180;
  var dLat = (b.lat - a.lat) * d2r, dLon = (b.lon - a.lon) * d2r;
  var s = Math.sin(dLat / 2), t = Math.sin(dLon / 2);
  return 2 * R * Math.asin(Math.sqrt(s * s + Math.cos(a.lat * d2r) * Math.cos(b.lat * d2r) * t * t));
}

// 在点位之间按距离加密采样，生成带经纬度的细化海拔剖面（确定性，刷新不变）
function buildProfile(route) {
  if (route._profile) return route._profile;
  var wps = route.waypoints || [];
  if (wps.length < 2) return [];
  var rand = _rng(_hashSeed(route.id || route.name || "r"));
  var segs = [], total = 0, i, j;
  for (i = 0; i < wps.length - 1; i++) {
    var d = Math.max(_havKm(wps[i], wps[i + 1]), 0.4);
    segs.push(d); total += d;
  }
  var prof = [], wpKm = [], acc = 0;
  for (i = 0; i < wps.length - 1; i++) {
    var a = wps[i], b = wps[i + 1];
    wpKm.push({ km: acc, wp: a });
    var n = Math.max(8, Math.round(segs[i] / total * 110));
    var amp = Math.min(Math.max(Math.abs(b.elevation - a.elevation) * 0.16, 12), 80);
    var f1 = 2 + rand() * 3, f2 = 5 + rand() * 5;
    var p1 = rand() * 6.28, p2 = rand() * 6.28;
    for (j = 0; j < n; j++) {
      var t = j / n;
      var base = a.elevation + (b.elevation - a.elevation) * (1 - Math.cos(Math.PI * t)) / 2;
      var env = Math.sin(Math.PI * t);   // 两端锁定在点位真实海拔
      var ele = base + env * (Math.sin(t * f1 * Math.PI + p1) * amp * 0.6 +
                              Math.sin(t * f2 * Math.PI + p2) * amp * 0.25);
      prof.push({
        lat: a.lat + (b.lat - a.lat) * t, lon: a.lon + (b.lon - a.lon) * t,
        ele: Math.round(ele), km: acc + segs[i] * t,
      });
    }
    acc += segs[i];
  }
  var last = wps[wps.length - 1];
  wpKm.push({ km: acc, wp: last });
  prof.push({ lat: last.lat, lon: last.lon, ele: last.elevation, km: acc });
  route._profile = prof; route._wpKm = wpKm; route._totalKm = acc;
  return prof;
}

function _cpLabel(wp) {
  var m = (wp.name || "").match(/CP\s*\d+/i);
  return m ? m[0].toUpperCase().replace(/\s+/g, "") : "CP";
}

function elevSVG(route) {
  var prof = buildProfile(route);
  if (prof.length < 2) return "";
  var w = 300, h = 56, pad = 4;
  var min = Infinity, max = -Infinity;
  prof.forEach(function (p) { if (p.ele < min) min = p.ele; if (p.ele > max) max = p.ele; });
  var span = Math.max(max - min, 1), total = route._totalKm;
  function X(km) { return pad + (w - 2 * pad) * km / total; }
  function Y(e) { return h - pad - (h - 2 * pad) * (e - min) / span; }
  var line = prof.map(function (p, i) {
    return (i ? "L" : "M") + X(p.km).toFixed(1) + " " + Y(p.ele).toFixed(1);
  }).join(" ");
  var area = line + " L" + (w - pad) + " " + h + " L" + pad + " " + h + " Z";
  var marks = "";
  (route._wpKm || []).forEach(function (m) {
    var k = m.wp.kind, x = X(m.km).toFixed(1), y = Y(m.wp.elevation).toFixed(1);
    if (k === "pass" || k === "peak") marks += '<circle cx="' + x + '" cy="' + y + '" r="3" fill="#ff7a45"/>';
    else if (k === "camp") marks += '<circle cx="' + x + '" cy="' + y + '" r="3" fill="#7ea6ff"/>';
    else if (k === "aid") marks += '<path d="M' + x + " " + y + ' v-9 l7 2.5 l-7 2.5" fill="#3ecf8e" stroke="#3ecf8e" stroke-width="1.2"/>';
  });
  return '<svg class="elev-svg" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' +
    '<path d="' + area + '" fill="rgba(255,122,69,.10)"/>' +
    '<path d="' + line + '" fill="none" stroke="#ff7a45" stroke-width="1.6" stroke-linejoin="round"/>' +
    marks + "</svg>";
}

// 大剖面图（计划页）：网格刻度 + CP 旗标 + 悬停/点选查看海拔经纬度
function profileChartSVG(route) {
  var prof = buildProfile(route);
  if (prof.length < 2) return "";
  var w = 640, h = 170, padL = 44, padR = 14, padT = 30, padB = 22;
  var plotW = w - padL - padR, plotH = h - padT - padB;
  var min = Infinity, max = -Infinity;
  prof.forEach(function (p) { if (p.ele < min) min = p.ele; if (p.ele > max) max = p.ele; });
  min = Math.floor((min - 30) / 100) * 100;
  max = Math.ceil((max + 30) / 100) * 100;
  var span = Math.max(max - min, 1), total = route._totalKm;
  state.pf = { padL: padL, padT: padT, plotW: plotW, plotH: plotH, min: min, span: span, total: total, w: w, h: h };
  function X(km) { return padL + plotW * km / total; }
  function Y(e) { return padT + plotH * (1 - (e - min) / span); }
  var line = prof.map(function (p, i) { return (i ? "L" : "M") + X(p.km).toFixed(1) + " " + Y(p.ele).toFixed(1); }).join(" ");
  var area = line + " L" + X(total).toFixed(1) + " " + (padT + plotH) + " L" + padL + " " + (padT + plotH) + " Z";
  var grid = "";
  for (var g = 0; g <= 3; g++) {
    var ge = min + span * g / 3, gy = Y(ge).toFixed(1);
    grid += '<line x1="' + padL + '" y1="' + gy + '" x2="' + (w - padR) + '" y2="' + gy + '" stroke="#2b3a5c" stroke-width="0.6" stroke-dasharray="3 4"/>' +
      '<text x="' + (padL - 6) + '" y="' + (parseFloat(gy) + 3) + '" text-anchor="end" font-size="9" fill="#8b98b8">' + Math.round(ge) + " m</text>";
  }
  var marks = "", isTrail = route.activity === "trailrun";
  (route._wpKm || []).forEach(function (m) {
    var wp = m.wp, x = X(m.km), y = Y(wp.elevation);
    if (wp.kind === "aid") {
      marks += '<line x1="' + x + '" y1="' + y + '" x2="' + x + '" y2="' + (y - 18) + '" stroke="#3ecf8e" stroke-width="1.4"/>' +
        '<path d="M' + x + " " + (y - 18) + ' l11 3.5 l-11 3.5 Z" fill="#3ecf8e"/>' +
        '<circle cx="' + x + '" cy="' + y + '" r="3" fill="#3ecf8e"/>' +
        (isTrail ? '<text x="' + x + '" y="' + (y - 22) + '" text-anchor="middle" font-size="9" font-weight="700" fill="#3ecf8e">' + esc(_cpLabel(wp)) + "</text>" : "");
    } else if (wp.kind === "pass" || wp.kind === "peak") {
      marks += '<circle cx="' + x + '" cy="' + y + '" r="3.4" fill="#ff7a45"/>';
    } else if (wp.kind === "camp") {
      marks += '<circle cx="' + x + '" cy="' + y + '" r="3.4" fill="#7ea6ff"/>';
    } else {
      marks += '<circle cx="' + x + '" cy="' + y + '" r="2.6" fill="#8b98b8"/>';
    }
  });
  return '<div class="profile-wrap">' +
    '<svg id="pfSvg" class="profile-chart" viewBox="0 0 ' + w + " " + h + '">' +
    grid +
    '<path d="' + area + '" fill="rgba(255,122,69,.10)"/>' +
    '<path d="' + line + '" fill="none" stroke="#ff7a45" stroke-width="1.8" stroke-linejoin="round"/>' +
    marks +
    '<g id="pfCross" class="hidden">' +
    '<line id="pfLine" y1="' + padT + '" y2="' + (padT + plotH) + '" stroke="#e8edf7" stroke-width="0.8" stroke-dasharray="2 3"/>' +
    '<circle id="pfDot" r="4" fill="#e8edf7" stroke="#ff7a45" stroke-width="2"/>' +
    "</g>" +
    '<rect x="' + padL + '" y="' + padT + '" width="' + plotW + '" height="' + plotH + '" fill="transparent" ' +
    'onmousemove="pfMove(event)" onclick="pfClick(event)" onmouseleave="pfLeave()"/>' +
    "</svg>" +
    '<div id="pfTip" class="profile-tip hidden"></div>' +
    '<p class="hint" style="margin-top:6px">鼠标滑过 / 点选剖面线查看海拔与经纬度，再次点击取消锁定' + (isTrail ? " · 🚩 绿旗为 CP 补给站" : "") + "</p>" +
    "</div>";
}

function _pfSample(ev) {
  var route = state.route, pf = state.pf;
  if (!route || !route._profile || !pf) return null;
  var svg = $("pfSvg"), rect = svg.getBoundingClientRect();
  var vx = (ev.clientX - rect.left) * pf.w / rect.width;
  var km = Math.min(Math.max((vx - pf.padL) / pf.plotW, 0), 1) * pf.total;
  var prof = route._profile, best = prof[0], bd = Infinity;
  for (var i = 0; i < prof.length; i++) {
    var d = Math.abs(prof[i].km - km);
    if (d < bd) { bd = d; best = prof[i]; }
  }
  return { s: best, rect: rect };
}

function _pfRender(hit) {
  var pf = state.pf, s = hit.s;
  var x = pf.padL + pf.plotW * s.km / pf.total;
  var y = pf.padT + pf.plotH * (1 - (s.ele - pf.min) / pf.span);
  $("pfCross").classList.remove("hidden");
  var lineEl = $("pfLine"); lineEl.setAttribute("x1", x); lineEl.setAttribute("x2", x);
  var dot = $("pfDot"); dot.setAttribute("cx", x); dot.setAttribute("cy", y);
  var near = "";
  (state.route._wpKm || []).forEach(function (m) {
    if (Math.abs(m.km - s.km) < Math.max(state.route._totalKm * 0.02, 0.3)) near = m.wp.name;
  });
  var tip = $("pfTip");
  tip.innerHTML = "<b>海拔 " + s.ele + " m</b> · 距起点 " + s.km.toFixed(1) + " km<br>" +
    "经纬度 " + s.lat.toFixed(4) + "°N, " + s.lon.toFixed(4) + "°E" +
    (near ? '<br><span class="hint">📍 ' + esc(near) + "</span>" : "");
  tip.classList.remove("hidden");
  var px = x * hit.rect.width / pf.w, py = y * hit.rect.height / pf.h;
  tip.style.left = Math.min(Math.max(px - 80, 0), hit.rect.width - 180) + "px";
  tip.style.top = (py > 84 ? py - 72 : py + 16) + "px";
}

function pfMove(ev) {
  if (state.pfPinned) return;
  var hit = _pfSample(ev);
  if (hit) _pfRender(hit);
}

function pfClick(ev) {
  var hit = _pfSample(ev);
  if (!hit) return;
  state.pfPinned = !state.pfPinned;
  _pfRender(hit);
}

function pfLeave() {
  if (state.pfPinned) return;
  $("pfCross").classList.add("hidden");
  $("pfTip").classList.add("hidden");
}

function renderRoutes(act) {
  var grid = $("routeGrid");
  var list = state.routes.filter(function (r) { return act === "all" || r.activity === act; });
  if (!list.length) { grid.innerHTML = '<p class="muted">暂无线路</p>'; return; }
  grid.innerHTML = list.map(function (r) {
    var risks = (r.waypoints || []).filter(function (p) { return p.risk; }).length;
    var actTag = r.activity === "trailrun"
      ? '<span class="act-tag act-trailrun">🏃 越野跑</span>'
      : '<span class="act-tag act-hiking">🥾 徒步 ' + r.days + ' 天</span>';
    return '<div class="route-card" onclick="selectRoute(\'' + esc(r.id) + '\')">' +
      '<div class="rc-top"><h3>' + esc(r.name) + "</h3>" + actTag + "</div>" +
      '<div class="rc-region">📍 ' + esc(r.region) + "</div>" +
      elevSVG(r) +
      '<div class="rc-stats">' +
      "<div><b>" + r.distance_km + "</b>公里</div>" +
      "<div><b>" + r.ascent_m + "</b>累计爬升 m</div>" +
      "<div><b>" + esc(r.difficulty) + "</b>难度</div>" +
      "</div>" +
      '<p class="rc-summary">' + esc(r.summary) + "</p>" +
      (risks ? '<div class="rc-risk">⚠ ' + risks + " 个风险点位已标注</div>" : "") +
      "</div>";
  }).join("");
}

function filterRoutes(act, btn) {
  document.querySelectorAll(".filter-row .chip").forEach(function (c) { c.classList.remove("active"); });
  btn.classList.add("active");
  renderRoutes(act);
}

function selectRoute(id) {
  var r = state.routes.find(function (x) { return x.id === id; });
  if (!r) return;
  state.route = r;
  state.gearItems = null;
  $("gearResult").innerHTML = "";
  $("createPlanBtn").classList.add("hidden");
  renderRouteSummary(r);
  showView("plan");
}

function renderRouteSummary(r) {
  var isTrail = r.activity === "trailrun";
  var wps = (r.waypoints || []).map(function (p) {
    var cp = isTrail && p.kind === "aid" ? ' <span class="cp-badge">🚩 ' + esc(_cpLabel(p)) + "</span>" : "";
    return '<div class="wp-item k-' + esc(p.kind) + '">' +
      '<div class="wp-name">D' + p.day + " · " + esc(p.name) + cp +
      ' <span class="hint">' + (KIND_LABEL[p.kind] || p.kind) + "</span></div>" +
      '<div class="wp-meta">海拔 ' + p.elevation + " m · " + p.lat.toFixed(4) + "°N, " + p.lon.toFixed(4) + "°E</div>" +
      (p.risk ? '<div class="wp-risk">⚠ ' + esc(p.risk) + "</div>" : "") +
      "</div>";
  }).join("");
  state.pfPinned = false;
  $("routeSummary").innerHTML =
    "<h3>" + esc(r.name) + "</h3>" +
    '<p class="muted" style="font-size:12px">' + esc(r.region) + " · " + r.distance_km +
    " km · 爬升 " + r.ascent_m + " m · " + esc(r.difficulty) + "</p>" +
    profileChartSVG(r) +
    '<div class="wp-list">' + wps + "</div>";
}

// ── GPX 导入 ──────────────────────────────────────────

function importGpx(input) {
  var file = input.files && input.files[0];
  if (!file) return;
  var name = prompt("给这条线路起个名字：", file.name.replace(/\.gpx$/i, "")) || "我的线路";
  var days = parseInt(prompt("计划走几天？", "3"), 10) || 3;
  var fd = new FormData();
  fd.append("file", file);
  fd.append("name", name);
  fd.append("activity", "hiking");
  fd.append("days", days);
  toast("正在解析 GPX 轨迹…");
  api("/api/routes/gpx", { method: "POST", body: fd }).then(function (route) {
    toast("导入成功：" + route.name + "（已采样 " + route.waypoints.length + " 个点位）");
    input.value = "";
    loadRoutes();
  }).catch(function (e) { toast("导入失败：" + e.message); input.value = ""; });
}

// ── 视图 2：装备解析 + 创建计划 ───────────────────────

function renderGearItems(items) {
  return items.map(function (g) {
    var params = [];
    var p = g.params || {};
    if (p.comfort_c != null) params.push("舒适温标 " + p.comfort_c + "°C");
    if (p.limit_c != null) params.push("极限 " + p.limit_c + "°C");
    if (p.waterproof_mm != null) params.push("防水 " + p.waterproof_mm + "mm");
    if (p.wind_ms != null) params.push("抗风 " + p.wind_ms + "m/s");
    if (p.weight_g != null) params.push(p.weight_g + "g");
    return '<div class="gear-item"><div>' +
      '<div class="g-name">' + esc(g.name) +
      ' <span class="hint">' + (CAT_LABEL[g.category] || g.category) + "</span></div>" +
      '<div class="g-params">' + (params.length ? esc(params.join(" · ")) : esc(g.note || "无关键参数")) + "</div>" +
      "</div>" +
      '<span class="conf conf-' + esc(g.confidence) + '">' + (CONF_LABEL[g.confidence] || g.confidence) + "</span>" +
      "</div>";
  }).join("");
}

function parseGear() {
  var text = $("gearText").value.trim();
  if (!text) { toast("先输入你的装备清单"); return; }
  var btn = $("parseGearBtn");
  btn.disabled = true;
  btn.textContent = "AI 识别中…（联网搜索装备参数）";
  postJSON("/api/gear/parse", { raw_text: text }).then(function (data) {
    state.gearItems = data.items || [];
    $("gearResult").innerHTML = state.gearItems.length
      ? renderGearItems(state.gearItems)
      : '<p class="muted">未识别出装备，请检查输入</p>';
    if (state.gearItems.length) $("createPlanBtn").classList.remove("hidden");
  }).catch(function (e) { toast("解析失败：" + e.message); })
    .finally(function () { btn.disabled = false; btn.textContent = "🔍 AI 识别装备参数"; });
}

function createPlan() {
  if (!state.route) return;
  var depart = $("departDate").value;
  if (!depart) { toast("请选择出发日期"); return; }
  var btn = $("createPlanBtn");
  btn.disabled = true;
  btn.textContent = "正在逐点查询沿线天气…";
  postJSON("/api/plans", {
    route_id: state.route.id,
    depart_date: depart,
    gear_raw_text: $("gearText").value,
    gear_items: state.gearItems,
  }).then(function (result) {
    state.plan = result.plan;
    state.report = result;
    renderReport(result);
    showView("report");
    toast("首份天气快照已生成 · 之后每次打开自动对账");
  }).catch(function (e) { toast("创建失败：" + e.message); })
    .finally(function () { btn.disabled = false; btn.textContent = "创建计划 · 生成首份天气快照"; });
}

// ── 视图 3：对账报告 ──────────────────────────────────

function reconcile(scenario) {
  if (!state.plan) { toast("请先创建或选择一个计划"); return; }
  $("loading").classList.remove("hidden");
  $("loadingText").textContent = scenario === "normal"
    ? "正在重查沿线天气并与上次快照对比…"
    : "正在注入演示情景并重新对账…";
  $("reportBody").classList.add("hidden");
  postJSON("/api/plans/" + state.plan.id + "/reconcile", { scenario: scenario || "normal" })
    .then(function (result) {
      state.report = result;
      renderReport(result);
      var diffs = (result.events || []).filter(function (e) { return e.kind.indexOf("gear_") !== 0; }).length;
      toast(result.has_diff
        ? (diffs ? "对账完成：检测到 " + diffs + " 处天气变化" : "对账完成：天气无显著变化 ✅")
        : "快照已更新");
    })
    .catch(function (e) { toast("对账失败：" + e.message); })
    .finally(function () {
      $("loading").classList.add("hidden");
      $("reportBody").classList.remove("hidden");
    });
}

function renderReport(r) {
  var route = state.routes.find(function (x) { return x.id === r.plan.route_id; }) || state.route || {};
  $("reportTitle").textContent = route.name || "对账报告";
  $("reportMeta").textContent = "出发 " + r.plan.depart_date +
    " · 装备 " + (r.plan.gear || []).length + " 件 · 对账于 " + fmtDate(r.reconciled_at) +
    (r.snapshot && r.snapshot.scenario !== "normal" ? " · 情景：" + r.snapshot.scenario : "");
  renderAlerts(r.events || []);
  renderAgentReport(r);
  renderMatrix(r, route);
}

function renderAlerts(events) {
  var box = $("alertList");
  if (!events.length) {
    box.innerHTML = '<div class="no-alerts">✅ 天气与装备对账通过，暂无需要处理的提醒</div>';
    return;
  }
  box.innerHTML = events.map(function (e) {
    var where = [];
    if (e.waypoint_name) where.push("📍 " + e.waypoint_name);
    if (e.date) where.push("📅 " + e.date);
    if (e.gear_affected && e.gear_affected.length) where.push("🎒 " + e.gear_affected.join("、"));
    return '<div class="alert-card alert-' + esc(e.severity) + '">' +
      '<div class="a-title">' + (SEV_ICON[e.severity] || "") + " " + esc(e.title) + "</div>" +
      (where.length ? '<div class="a-where">' + esc(where.join("　")) + "</div>" : "") +
      '<div class="a-detail">' + esc(e.detail) + "</div>" +
      (e.suggestion ? '<div class="a-sug"><b>建议：</b>' + esc(e.suggestion) + "</div>" : "") +
      "</div>";
  }).join("");
}

// 极简 Markdown → HTML（只处理报告用到的子集）
function mdToHtml(md) {
  var lines = String(md || "").split("\n");
  var html = [], inList = false;
  lines.forEach(function (line) {
    var l = line.trim();
    var isLi = /^[-*]\s+/.test(l);
    if (inList && !isLi) { html.push("</ul>"); inList = false; }
    if (!l) return;
    var t = esc(l.replace(/^#+\s*/, "").replace(/^[-*]\s+/, ""))
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    if (/^###?\s/.test(l) || /^#\s/.test(l)) { html.push("<h3>" + t + "</h3>"); }
    else if (isLi) {
      if (!inList) { html.push("<ul>"); inList = true; }
      html.push("<li>" + t + "</li>");
    } else { html.push("<p>" + t + "</p>"); }
  });
  if (inList) html.push("</ul>");
  return html.join("");
}

function renderAgentReport(r) {
  var tag = r.generated_by === "llm"
    ? "🤖 由 AI Agent 结合天气快照与装备参数生成"
    : "📋 模板报告（配置 MODELSCOPE_API_KEY 后启用 AI 生成）";
  $("agentReport").innerHTML = mdToHtml(r.report_md) + '<div class="gen-tag">' + tag + "</div>";
}

function renderMatrix(r, route) {
  var snap = r.snapshot;
  var prev = r.prev_snapshot;
  $("snapTime").textContent = "快照 " + fmtDate(snap.taken_at);
  var wpMap = {};
  (route.waypoints || []).forEach(function (p) { wpMap[p.id] = p; });
  var prevMap = {};
  if (prev) (prev.cells || []).forEach(function (c) { prevMap[c.waypoint_id + "|" + c.date] = c; });

  function delta(cur, old, unit, invert) {
    if (old == null || cur == null) return "";
    var d = Math.round((cur - old) * 10) / 10;
    if (Math.abs(d) < 0.5) return "";
    var up = d > 0;
    var bad = invert ? !up : up;
    return '<span class="delta ' + (bad ? "up" : "down") + '">' + (up ? "▲" : "▼") + Math.abs(d) + unit + "</span>";
  }

  var rows = (snap.cells || []).map(function (c) {
    var wp = wpMap[c.waypoint_id] || {};
    var old = prevMap[c.waypoint_id + "|" + c.date];
    var tminCls = c.t_min <= 0 ? "t-cold" : "t-warm";
    var windCls = c.ws10m_max >= 10.8 ? "wind-hi" : "";
    var rainCls = c.tp_mm >= 1 ? "rain-yes" : "muted";
    return "<tr>" +
      '<td><div class="wxm-wp">D' + (wp.day || "?") + " " + esc(wp.name || c.waypoint_id) + "</div>" +
      '<div class="wxm-elev">' + (wp.elevation != null ? wp.elevation + " m" : "") + "</div></td>" +
      "<td>" + dayLabel(c.date) + "</td>" +
      '<td><span class="' + tminCls + '">' + c.t_min + "°</span>" +
      "/" + c.t_max + "°" + (old ? delta(c.t_min, old.t_min, "°", true) : "") + "</td>" +
      '<td class="' + windCls + '">' + c.ws10m_max + " m/s " + esc(c.wd10m || "") +
      (old ? delta(c.ws10m_max, old.ws10m_max, "", false) : "") + "</td>" +
      '<td class="' + rainCls + '">' + (c.tp_mm >= 0.5 ? c.tp_mm + " mm" : "—") +
      (old ? delta(c.tp_mm, old.tp_mm, "mm", false) : "") + "</td>" +
      "<td>" + c.rh2m_avg + "%</td>" +
      "</tr>";
  }).join("");

  $("wxMatrix").innerHTML =
    "<table><thead><tr><th>点位</th><th>日期</th><th>最低/最高温</th><th>最大风</th><th>降水</th><th>湿度</th></tr></thead>" +
    "<tbody>" + rows + "</tbody></table>" +
    (prev ? '<p class="hint" style="margin-top:8px">▲▼ 为与上次快照（' + fmtDate(prev.taken_at) + '）的变化量</p>' : "");
}

// ── 视图 4：模拟推送 ──────────────────────────────────

function showPush() {
  if (!state.report) { toast("先完成一次对账"); return; }
  var now = new Date();
  $("phoneTime").textContent = String(now.getHours()).padStart(2, "0") + ":" + String(now.getMinutes()).padStart(2, "0");
  var events = (state.report.events || []).slice(0, 5);
  var cards = events.length ? events.map(function (e, i) {
    return '<div class="push-card" style="animation-delay:' + (i * 0.18) + 's">' +
      '<div class="p-app"><span class="p-dot"></span>行山对账 · 刚刚</div>' +
      '<div class="p-title">' + (SEV_ICON[e.severity] || "") + " " + esc(e.title) + "</div>" +
      '<div class="p-body">' + esc(e.detail) + (e.suggestion ? " 建议：" + esc(e.suggestion) : "") + "</div>" +
      "</div>";
  }).join("") :
    '<div class="push-card"><div class="p-app"><span class="p-dot"></span>行山对账 · 刚刚</div>' +
    '<div class="p-title">✅ 出行窗口天气稳定</div>' +
    '<div class="p-body">沿线各点位天气与上次核对无显著变化，按原计划准备即可。</div></div>';
  $("pushList").innerHTML = cards;
  showView("push");
}

// ── 我的计划抽屉 ──────────────────────────────────────

function showMyPlans() {
  $("planDrawer").classList.remove("hidden");
  $("planList").innerHTML = '<p class="muted">加载中…</p>';
  api("/api/plans").then(function (data) {
    var plans = data.plans || [];
    if (!plans.length) {
      $("planList").innerHTML = '<p class="muted">还没有计划 · 先去选一条线路吧</p>';
      return;
    }
    $("planList").innerHTML = plans.map(function (p) {
      return '<div class="plan-row" onclick="openPlan(\'' + esc(p.id) + '\')">' +
        '<div class="pr-name">' + esc(p.route_name) + "</div>" +
        '<div class="pr-meta">出发 ' + esc(p.depart_date) + " · 装备 " + (p.gear || []).length +
        " 件 · 快照 " + (p.snapshots || []).length + " 份</div>" +
        "</div>";
    }).join("");
  }).catch(function (e) { $("planList").innerHTML = '<p class="muted">加载失败：' + esc(e.message) + "</p>"; });
}

function closeDrawer() { $("planDrawer").classList.add("hidden"); }

function openPlan(planId) {
  closeDrawer();
  api("/api/plans/" + planId + "/report").then(function (r) {
    state.plan = r.plan;
    state.report = r;
    var route = state.routes.find(function (x) { return x.id === r.plan.route_id; });
    if (route) state.route = route;
    renderReport(r);
    showView("report");
    // 打开即对账：自动重查一次
    reconcile("normal");
  }).catch(function (e) { toast("加载失败：" + e.message); });
}

$("planDrawer").addEventListener("click", function (ev) {
  if (ev.target === this) closeDrawer();
});

init();

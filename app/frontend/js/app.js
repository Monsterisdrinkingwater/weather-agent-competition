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

function elevSVG(wps) {
  if (!wps || wps.length < 2) return "";
  var w = 300, h = 56, pad = 4;
  var elevs = wps.map(function (p) { return p.elevation; });
  var min = Math.min.apply(null, elevs), max = Math.max.apply(null, elevs);
  var span = Math.max(max - min, 1);
  var pts = wps.map(function (p, i) {
    var x = pad + (w - 2 * pad) * i / (wps.length - 1);
    var y = h - pad - (h - 2 * pad) * (p.elevation - min) / span;
    return [x, y, p];
  });
  var line = pts.map(function (p, i) { return (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); }).join(" ");
  var area = line + " L" + (w - pad) + " " + h + " L" + pad + " " + h + " Z";
  var dots = pts.filter(function (p) { return p[2].kind === "pass" || p[2].kind === "peak" || p[2].kind === "camp"; })
    .map(function (p) {
      var col = p[2].kind === "camp" ? "#7ea6ff" : "#ff7a45";
      return '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="3" fill="' + col + '"/>';
    }).join("");
  return '<svg class="elev-svg" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' +
    '<path d="' + area + '" fill="rgba(255,122,69,.10)"/>' +
    '<path d="' + line + '" fill="none" stroke="#ff7a45" stroke-width="1.8" stroke-linejoin="round"/>' +
    dots + "</svg>";
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
      elevSVG(r.waypoints) +
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
  var wps = (r.waypoints || []).map(function (p) {
    return '<div class="wp-item k-' + esc(p.kind) + '">' +
      '<div class="wp-name">D' + p.day + " · " + esc(p.name) +
      ' <span class="hint">' + (KIND_LABEL[p.kind] || p.kind) + "</span></div>" +
      '<div class="wp-meta">海拔 ' + p.elevation + " m</div>" +
      (p.risk ? '<div class="wp-risk">⚠ ' + esc(p.risk) + "</div>" : "") +
      "</div>";
  }).join("");
  $("routeSummary").innerHTML =
    "<h3>" + esc(r.name) + "</h3>" +
    '<p class="muted" style="font-size:12px">' + esc(r.region) + " · " + r.distance_km +
    " km · 爬升 " + r.ascent_m + " m · " + esc(r.difficulty) + "</p>" +
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

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
  // ── 对话式 Agent（阶段一）
  conversation: null,       // 当前对话 {id, messages:[], plan_id}
  chatBusy: false,          // 是否正在等待回复
  chatAttach: [],           // 待发送附件 [{kind:'gpx'|'image', file?, dataUrl?, name}]
  producedPlans: [],        // 本次对话产出的计划 [{plan_id, route_name, depart_date}]
  liveStream: null,         // 实时监测 SSE 连接（阶段二）
  lastUserText: "",         // 最近一条用户消息（主题引擎提取日期用）
};

var KIND_LABEL = { start: "起点", pass: "垭口", camp: "营地", peak: "山顶", water: "水源/横渡", finish: "终点", aid: "补给站" };
var CAT_LABEL = { sleep: "睡眠", shelter: "帐篷", rain: "防雨", warm: "保暖", footwear: "鞋袜", other: "其他" };
var CONF_LABEL = { high: "联网核实", medium: "AI 估计", low: "内置知识库" };
var SOURCE_LABEL = { gear_db: "装备库", user: "已确认", web_search: "联网检索", llm_estimate: "AI 估计", unknown: "待补参" };
// 各类别可编辑的参数字段：[key, 标签, 是否数字]
var PARAM_FIELDS = {
  sleep: [["comfort_c", "舒适温标 °C", true], ["limit_c", "极限温标 °C", true], ["weight_g", "重量 g", true]],
  shelter: [["wind_ms", "抗风 m/s", true], ["waterproof_mm", "外帐防水 mm", true]],
  rain: [["waterproof_mm", "静水压 mm", true], ["breathability_g", "透气 g/m²/24h", true]],
  warm: [["rating_c", "适温 °C", true], ["fill_g", "充绒量 g", true]],
  footwear: [["lug_mm", "齿深 mm", true]],
  other: []
};
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
  ["chat", "plan", "report", "push", "live"].forEach(function (v) {
    var el = $("view-" + v);
    if (el) el.classList.toggle("hidden", v !== name);
  });
  // 手机 App 版底部导航高亮：计划相关视图都归到“计划”Tab
  var activeTab = name === "chat" ? "tab-chat" : "tab-plans";
  ["tab-chat", "tab-plans"].forEach(function (id) {
    var t = $(id);
    if (t) t.classList.toggle("active", id === activeTab);
  });
  // 切视图时恢复对应语境的天气主题：对话聊的地方 vs 计划/报告的线路
  var wxCtx = _wxCtx[name === "chat" ? "chat" : "plan"];
  if (wxCtx) applyWxTheme(wxCtx[0], wxCtx[1], wxCtx[2], wxCtx[3]);
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
    // 对话 badge：LLM 是否可用
    var cb = $("chatBadge");
    if (cb) {
      cb.textContent = m.llm_enabled ? "🤖 AI 在线" : "⚠ 需配置 LLM Key";
    }
  }).catch(function () { $("sourceBadge").textContent = "离线"; });

  loadRoutes();
  showView("chat");

  // 出发日期默认 +3 天；改日期时同步刷新天气主题
  var d = new Date(Date.now() + 3 * 86400000);
  $("departDate").value = _isoLocal(d);
  $("departDate").addEventListener("change", function () {
    if (state.route) applyRouteWx(state.route, this.value);
  });

  // 打开即按用户当前位置的实时天气渲染背景（拒绝定位则保持默认主题）
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(function (pos) {
      applyWxTheme(pos.coords.latitude, pos.coords.longitude, "", "当前位置", "chat");
    }, function () {}, { timeout: 8000, maximumAge: 600000 });
  }
}

// ── 天气自适应主题（数据：中科天机；风格参考和风天气）────

var WX_META = {
  clear: { ic: "☀️", label: "晴" }, cloudy: { ic: "⛅", label: "多云" },
  overcast: { ic: "☁️", label: "阴" }, rain: { ic: "🌧", label: "下雨" },
  heavyrain: { ic: "⛈", label: "强降水" }, snow: { ic: "🌨", label: "下雪" },
  fog: { ic: "🌫", label: "雾" },
};

function _isoLocal(d) {
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
    "-" + String(d.getDate()).padStart(2, "0");
}

// 各语境最后一次天气主题参数：对话（当前位置/聊到的线路） vs 计划报告（选定线路）
var _wxCtx = { chat: null, plan: null };
var _wxCurQ = "";   // 当前已应用的查询参数，切视图恢复时相同则不重复请求

// 拉单点天气主题并应用：背景渐变 + 顶栏徽标标注（实时/预报）；ctx 记录归属语境
function applyWxTheme(lat, lon, dateStr, placeLabel, ctx) {
  if (ctx) _wxCtx[ctx] = [lat, lon, dateStr, placeLabel];
  var q = "/api/weather/theme?lat=" + Number(lat).toFixed(4) +
    "&lon=" + Number(lon).toFixed(4) +
    (dateStr ? "&target_date=" + dateStr : "");
  if (q === _wxCurQ) return Promise.resolve();
  _wxCurQ = q;
  return api(q).then(function (w) {
    document.body.dataset.wx = w.category;
    document.body.classList.toggle("wx-night", !!w.is_night);
    _renderWxFx(w.category, !!w.is_night);
    // 重新触发背景淡入动画
    document.body.classList.remove("wx-anim");
    void document.body.offsetWidth;
    document.body.classList.add("wx-anim");
    var meta = WX_META[w.category] || { ic: "🌤", label: w.category };
    var badge = $("wxBadge");
    if (badge) {
      var srcTag = w.mode === "forecast" ? dayLabel(w.date) + " 预报" : "实时";
      badge.innerHTML = meta.ic + " " + Math.round(w.temp_c) + "° " + esc(placeLabel || "") +
        " <i>" + meta.label + " · " + srcTag + "</i>";
      badge.classList.remove("hidden");
    }
  }).catch(function () { _wxCurQ = ""; /* 主题查询失败静默降级，下次可重试 */ });
}

// ── 天气粒子层：雨滴/云/太阳/雪/雾/星月（样式见 style.css 的 fx-*）

var _wxFxKey = "";   // 同一天气不重建，避免重复刷主题时粒子闪烁

function _renderWxFx(category, isNight) {
  var box = $("wxFx");
  if (!box) return;
  var key = category + (isNight ? "|n" : "|d");
  if (key === _wxFxKey) return;
  _wxFxKey = key;
  var html = [], i, n;

  function drops(count, minDur, maxDur) {
    for (i = 0; i < count; i++) {
      var dur = (minDur + Math.random() * (maxDur - minDur)).toFixed(2);
      html.push('<i class="fx-drop" style="left:' + (Math.random() * 104 - 2).toFixed(1) +
        "%;animation-duration:" + dur + "s;animation-delay:-" + (Math.random() * dur).toFixed(2) +
        "s;opacity:" + (0.35 + Math.random() * 0.5).toFixed(2) +
        ";height:" + (10 + Math.random() * 8).toFixed(0) + 'vh"></i>');
    }
  }
  function clouds(count, alphaScale) {
    for (i = 0; i < count; i++) {
      var w = 140 + Math.random() * 180;
      var dur = 65 + Math.random() * 70;
      html.push('<i class="fx-cloud" style="top:' + (2 + Math.random() * 30).toFixed(1) +
        "%;width:" + w.toFixed(0) + "px;height:" + (w * 0.5).toFixed(0) +
        "px;animation-duration:" + dur.toFixed(0) + "s;animation-delay:-" + (Math.random() * dur).toFixed(0) +
        "s;opacity:" + ((0.5 + Math.random() * 0.4) * (alphaScale || 1)).toFixed(2) + '"></i>');
    }
  }

  if (category === "rain") { clouds(3, 0.7); drops(34, 0.9, 1.6); }
  else if (category === "heavyrain") { clouds(4, 0.8); drops(60, 0.55, 1.0); }
  else if (category === "snow") {
    for (i = 0; i < 32; i++) {
      var sz = (2 + Math.random() * 4).toFixed(1);
      var sd = (6 + Math.random() * 8).toFixed(2);
      html.push('<i class="fx-flake" style="left:' + (Math.random() * 100).toFixed(1) +
        "%;width:" + sz + "px;height:" + sz + "px;animation-duration:" + sd +
        "s;animation-delay:-" + (Math.random() * sd).toFixed(2) +
        "s;opacity:" + (0.4 + Math.random() * 0.6).toFixed(2) + '"></i>');
    }
  }
  else if (category === "cloudy") { clouds(4, 1); if (!isNight) html.push('<i class="fx-sun" style="opacity:.6"></i>'); }
  else if (category === "overcast") { clouds(6, 1); }
  else if (category === "fog") {
    for (i = 0; i < 3; i++) {
      html.push('<i class="fx-mist" style="top:' + (12 + i * 26) + "%;height:" + (16 + Math.random() * 10).toFixed(0) +
        "vh;animation-duration:" + (9 + i * 4) + "s;animation-delay:-" + (i * 3) + 's"></i>');
    }
  }
  else if (category === "clear") { html.push(isNight ? "" : '<i class="fx-sun"></i>'); }

  // 夜间通用：星星（雨雪天不加），晴/多云夜再加月亮
  if (isNight && category !== "rain" && category !== "heavyrain" && category !== "snow") {
    n = category === "clear" ? 46 : 22;
    for (i = 0; i < n; i++) {
      var st = (1 + Math.random() * 1.6).toFixed(1);
      html.push('<i class="fx-star" style="left:' + (Math.random() * 100).toFixed(1) +
        "%;top:" + (Math.random() * 55).toFixed(1) + "%;width:" + st + "px;height:" + st +
        "px;animation-duration:" + (1.6 + Math.random() * 2.4).toFixed(2) +
        "s;animation-delay:-" + (Math.random() * 3).toFixed(2) + 's"></i>');
    }
    if (category === "clear" || category === "cloudy") html.push('<i class="fx-moon"></i>');
  }
  box.innerHTML = html.join("");
}

// 按线路切主题：取首个点位坐标；日期在预报期内后端自动给预报，否则给实时
function applyRouteWx(route, dateStr, ctx) {
  var wp = route && (route.waypoints || [])[0];
  if (!wp) return;
  applyWxTheme(wp.lat, wp.lon, dateStr || "", route.name, ctx || "plan");
}

// 从用户消息里提取出行日期（聊线路时主题跟着提到的日期走）
function _extractDateFromText(text) {
  if (!text) return "";
  var now = new Date();
  var m = text.match(/(\d{4})[-\/年](\d{1,2})[-\/月](\d{1,2})/);
  var y, mo, d;
  if (m) { y = +m[1]; mo = +m[2]; d = +m[3]; }
  else if ((m = text.match(/(\d{1,2})月(\d{1,2})[日号]/))) {
    y = now.getFullYear(); mo = +m[1]; d = +m[2];
    // 已过去的月日视为明年
    if (mo < now.getMonth() + 1 || (mo === now.getMonth() + 1 && d < now.getDate())) y += 1;
  } else if (text.indexOf("大后天") >= 0) { return _isoLocal(new Date(Date.now() + 3 * 86400000)); }
  else if (text.indexOf("后天") >= 0) { return _isoLocal(new Date(Date.now() + 2 * 86400000)); }
  else if (text.indexOf("明天") >= 0) { return _isoLocal(new Date(Date.now() + 86400000)); }
  else if (text.indexOf("今天") >= 0) { return _isoLocal(now); }
  else return "";
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return "";
  return y + "-" + String(mo).padStart(2, "0") + "-" + String(d).padStart(2, "0");
}

function loadRoutes() {
  // 线路表供对话卡片/建计划/报告剖面使用（页面不再有线路列表视图）
  return api("/api/routes").then(function (data) {
    state.routes = data.routes || [];
  }).catch(function (e) { toast("线路加载失败：" + e.message); });
}

// ── 海拔剖面（对话卡片缩略图 + 计划/报告大图）──────────

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
    if (k === "pass" || k === "peak") marks += '<circle cx="' + x + '" cy="' + y + '" r="3" fill="#FF9500"/>';
    else if (k === "camp") marks += '<circle cx="' + x + '" cy="' + y + '" r="3" fill="#007AFF"/>';
    else if (k === "aid") marks += '<path d="M' + x + " " + y + ' v-9 l7 2.5 l-7 2.5" fill="#34C759" stroke="#34C759" stroke-width="1.2"/>';
  });
  return '<svg class="elev-svg" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' +
    '<path d="' + area + '" fill="rgba(255,149,0,.12)"/>' +
    '<path d="' + line + '" fill="none" stroke="#FF9500" stroke-width="1.6" stroke-linejoin="round"/>' +
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
    grid += '<line x1="' + padL + '" y1="' + gy + '" x2="' + (w - padR) + '" y2="' + gy + '" stroke="#E0E0E5" stroke-width="0.6" stroke-dasharray="3 4"/>' +
      '<text x="' + (padL - 6) + '" y="' + (parseFloat(gy) + 3) + '" text-anchor="end" font-size="9" fill="#8E8E93">' + Math.round(ge) + " m</text>";
  }
  var marks = "", isTrail = route.activity === "trailrun";
  (route._wpKm || []).forEach(function (m) {
    var wp = m.wp, x = X(m.km), y = Y(wp.elevation);
    if (wp.kind === "aid") {
      marks += '<line x1="' + x + '" y1="' + y + '" x2="' + x + '" y2="' + (y - 18) + '" stroke="#34C759" stroke-width="1.4"/>' +
        '<path d="M' + x + " " + (y - 18) + ' l11 3.5 l-11 3.5 Z" fill="#34C759"/>' +
        '<circle cx="' + x + '" cy="' + y + '" r="3" fill="#34C759"/>' +
        (isTrail ? '<text x="' + x + '" y="' + (y - 22) + '" text-anchor="middle" font-size="9" font-weight="700" fill="#34C759">' + esc(_cpLabel(wp)) + "</text>" : "");
    } else if (wp.kind === "pass" || wp.kind === "peak") {
      marks += '<circle cx="' + x + '" cy="' + y + '" r="3.4" fill="#FF9500"/>';
    } else if (wp.kind === "camp") {
      marks += '<circle cx="' + x + '" cy="' + y + '" r="3.4" fill="#007AFF"/>';
    } else {
      marks += '<circle cx="' + x + '" cy="' + y + '" r="2.6" fill="#8E8E93"/>';
    }
  });
  return '<div class="profile-wrap">' +
    '<svg id="pfSvg" class="profile-chart" viewBox="0 0 ' + w + " " + h + '">' +
    grid +
    '<path d="' + area + '" fill="rgba(255,149,0,.12)"/>' +
    '<path d="' + line + '" fill="none" stroke="#FF9500" stroke-width="1.8" stroke-linejoin="round"/>' +
    marks +
    '<g id="pfCross" class="hidden">' +
    '<line id="pfLine" y1="' + padT + '" y2="' + (padT + plotH) + '" stroke="#6E6E73" stroke-width="0.8" stroke-dasharray="2 3"/>' +
    '<circle id="pfDot" r="4" fill="#FFFFFF" stroke="#FF9500" stroke-width="2"/>' +
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

function selectRoute(id) {
  var r = state.routes.find(function (x) { return x.id === id; });
  if (!r) return;
  state.route = r;
  state.gearItems = null;
  $("gearResult").innerHTML = "";
  _refreshGearUI();
  renderRouteSummary(r);
  applyRouteWx(r, $("departDate").value);   // 背景切到该线路出发日天气
  showView("plan");
}

function renderRouteSummary(r) {
  var isTrail = r.activity === "trailrun";
  var wps = (r.waypoints || []).map(function (p) {
    var cp = isTrail && p.kind === "aid" ? ' <span class="cp-badge">🚩 ' + esc(_cpLabel(p)) + "</span>" : "";
    return '<div class="wp-item k-' + esc(p.kind) + '">' +
      '<div class="wp-name">Day ' + p.day + " · " + esc(p.name) + cp +
      ' <span class="hint">' + (KIND_LABEL[p.kind] || p.kind) + "</span></div>" +
      '<div class="wp-meta">海拔 ' + p.elevation + " m · " + p.lat.toFixed(4) + "°N, " + p.lon.toFixed(4) + "°E</div>" +
      (p.risk ? '<div class="wp-risk">⚠ ' + esc(p.risk) + "</div>" : "") +
      "</div>";
  }).join("");
  state.pfPinned = false;
  // 剖面大图内部元素 id 固定（pfSvg 等），计划页渲染时清掉报告页的剖面，避免 id 重复
  var rp = $("routeProfile");
  if (rp) rp.innerHTML = "";
  $("routeSummary").innerHTML =
    "<h3>" + esc(r.name) + "</h3>" +
    '<p class="muted" style="font-size:12px">' + esc(r.region) + " · " + r.distance_km +
    " km · 爬升 " + r.ascent_m + " m · " + esc(r.difficulty) + "</p>" +
    profileChartSVG(r) +
    '<div class="wp-list">' + wps + "</div>";
}

// ── 对话内附件：GPX 轨迹 / 图片（VLM 识别）─────────────

// 图片统一转码成 JPEG：macOS/iPhone 默认 HEIC 直接发给模型会 400，
// 过大的图也顺便缩到长边 1280px，防止请求体超限
function _fileToJpegDataUrl(file) {
  return new Promise(function (resolve, reject) {
    var url = URL.createObjectURL(file);
    var img = new Image();
    img.onload = function () {
      URL.revokeObjectURL(url);
      var MAX = 1280;
      var scale = Math.min(1, MAX / Math.max(img.width, img.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(img.width * scale));
      canvas.height = Math.max(1, Math.round(img.height * scale));
      var ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff";    // 透明 PNG 转 JPEG 时垫白底
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      resolve(canvas.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = function () {
      URL.revokeObjectURL(url);
      reject(new Error("浏览器无法解码该图片，请转成 JPG/PNG 后再发"));
    };
    img.src = url;
  });
}

function onChatFiles(input) {
  var files = Array.prototype.slice.call(input.files || []);
  input.value = "";
  files.forEach(function (f) {
    if (/\.(gpx|kml|kmz)$/i.test(f.name)) {
      state.chatAttach.push({ kind: "gpx", file: f, name: f.name });
      _renderAttachPreview();
    } else if ((f.type || "").indexOf("image/") === 0 || /\.(heic|heif)$/i.test(f.name)) {
      _fileToJpegDataUrl(f).then(function (dataUrl) {
        state.chatAttach.push({ kind: "image", dataUrl: dataUrl, name: f.name });
        _renderAttachPreview();
      }).catch(function (e) { toast(f.name + "：" + e.message); });
    } else {
      toast("暂不支持该文件类型：" + f.name);
    }
  });
}

function removeAttach(i) {
  state.chatAttach.splice(i, 1);
  _renderAttachPreview();
}

function _renderAttachPreview() {
  var box = $("attachPreview");
  if (!box) return;
  if (!state.chatAttach.length) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  box.innerHTML = state.chatAttach.map(function (a, i) {
    var body = a.kind === "image"
      ? '<img src="' + a.dataUrl + '" alt="">'
      : '<span class="ac-ic">🗺</span>';
    return '<span class="attach-chip">' + body +
      '<span class="ac-name">' + esc(a.name) + "</span>" +
      '<button class="ac-del" onclick="removeAttach(' + i + ')" title="移除">✕</button></span>';
  }).join("");
}

// 上传所有轨迹附件（GPX/KML/KMZ），返回给 Agent 的系统提示行（导入后立即可被 search_routes 搜到）
function _uploadGpxAll(gpxes) {
  if (!gpxes.length) return Promise.resolve([]);
  toast("正在解析轨迹文件…");
  var jobs = gpxes.map(function (a) {
    var fd = new FormData();
    fd.append("file", a.file);
    fd.append("name", a.name.replace(/\.(gpx|kml|kmz)$/i, ""));
    fd.append("activity", "hiking");
    fd.append("days", 3);
    return api("/api/routes/gpx", { method: "POST", body: fd }).then(function (route) {
      return "[系统提示] 我上传了轨迹文件「" + route.name + "」，已导入为线路 route_id=" + route.id +
        "（" + route.distance_km + " km / 爬升 " + route.ascent_m + " m），请基于这条线路继续。";
    }).catch(function (e) {
      toast("轨迹导入失败：" + e.message);
      return "";
    });
  });
  return Promise.all(jobs).then(function (notes) {
    return loadRoutes().then(function () {   // 刷新本地线路表，对话卡片剖面图要用
      return notes.filter(Boolean);
    });
  });
}

// ── 视图 2：装备解析 + 创建计划 ───────────────────────

function _gearSummaryParams(p) {
  var params = [];
  if (p.comfort_c != null) params.push("舒适温标 " + p.comfort_c + "°C");
  if (p.limit_c != null) params.push("极限 " + p.limit_c + "°C");
  if (p.waterproof_mm != null) params.push("防水 " + p.waterproof_mm + "mm");
  if (p.breathability_g != null) params.push("透气 " + p.breathability_g + "g/m²/24h");
  if (p.wind_ms != null) params.push("抗风 " + p.wind_ms + "m/s");
  if (p.rating_c != null) params.push("适温 " + p.rating_c + "°C");
  if (p.lug_mm != null) params.push("齿深 " + p.lug_mm + "mm");
  if (p.lumens != null) params.push(p.lumens + " 流明");
  if (p.weight_g != null) params.push(p.weight_g + "g");
  return params;
}

function renderGearItems(items) {
  return items.map(function (g, idx) {
    var src = g.param_source || "unknown";
    var srcBadge = '<span class="g-src g-src-' + esc(src) + '">' + (SOURCE_LABEL[src] || src) + "</span>";
    if (g.needs_review) {
      // 未命中装备库/缺必填参数 → 可编辑补参行（AI 估计值预填）
      var fields = PARAM_FIELDS[g.category] || [];
      var inputs = fields.map(function (f) {
        var val = (g.params || {})[f[0]];
        return '<label class="gp-field"><span>' + esc(f[1]) + "</span>" +
          '<input type="number" step="any" id="gp-' + idx + "-" + f[0] + '"' +
          (val != null ? ' value="' + esc(String(val)) + '"' : "") + "></label>";
      }).join("");
      return '<div class="gear-item gear-edit">' +
        '<div class="g-head"><span class="g-name">' + esc(g.name) +
        ' <span class="hint">' + (CAT_LABEL[g.category] || g.category) + "</span></span>" + srcBadge + "</div>" +
        '<div class="g-note">' + esc(g.note || "未在装备库找到，请确认参数") + "</div>" +
        (inputs
          ? '<div class="gp-fields">' + inputs + "</div>"
          : '<div class="g-note">该类装备无需量化参数，点确认即可</div>') +
        '<button class="ghost-btn gp-confirm" onclick="confirmGear(' + idx + ')">✓ 确认参数并存入装备库</button>' +
        "</div>";
    }
    var params = _gearSummaryParams(g.params || {});
    return '<div class="gear-item"><div>' +
      '<div class="g-name">' + esc(g.name) +
      ' <span class="hint">' + (CAT_LABEL[g.category] || g.category) + "</span></div>" +
      '<div class="g-params">' + (params.length ? esc(params.join(" · ")) : esc(g.note || "无关键参数")) + "</div>" +
      "</div>" + srcBadge + "</div>";
  }).join("");
}

// 必填参数（与后端 gear_db.REQUIRED_PARAMS 保持一致）
var REQUIRED_PARAM = { sleep: "comfort_c", rain: "waterproof_mm", shelter: "wind_ms", warm: "rating_c" };

function _refreshGearUI() {
  var items = state.gearItems || [];
  $("gearResult").innerHTML = items.length
    ? renderGearItems(items)
    : "";
  var pending = items.filter(function (g) { return g.needs_review; }).length;
  var btn = $("createPlanBtn");
  // 装备可空：没解析装备也能建计划，之后在报告页/对话里补
  btn.classList.remove("hidden");
  btn.disabled = pending > 0;
  btn.textContent = pending > 0
    ? "待确认 " + pending + " 件装备"
    : "创建计划";
}

function confirmGear(idx) {
  var g = state.gearItems[idx];
  if (!g) return;
  var fields = PARAM_FIELDS[g.category] || [];
  var params = {};
  var p = g.params || {};
  for (var k in p) if (p.hasOwnProperty(k)) params[k] = p[k];
  for (var i = 0; i < fields.length; i++) {
    var el = $("gp-" + idx + "-" + fields[i][0]);
    if (!el) continue;
    var v = el.value.trim();
    if (v === "") { delete params[fields[i][0]]; continue; }
    params[fields[i][0]] = parseFloat(v);
  }
  var reqKey = REQUIRED_PARAM[g.category];
  if (reqKey && params[reqKey] == null) {
    toast("请先填写必填参数：" + (PARAM_FIELDS[g.category][0] ? PARAM_FIELDS[g.category][0][1] : reqKey));
    return;
  }
  postJSON("/api/gear/confirm", { name: g.name, category: g.category, params: params })
    .then(function (item) {
      state.gearItems[idx] = item;
      _refreshGearUI();
      toast("已确认并存入装备库：" + item.name);
    })
    .catch(function (e) { toast("确认失败：" + e.message); });
}

function parseGear() {
  var text = $("gearText").value.trim();
  if (!text) { toast("先输入你的装备清单"); return; }
  var btn = $("parseGearBtn");
  btn.disabled = true;
  btn.textContent = "正在匹配装备库…（未命中的由 AI 预估参数）";
  postJSON("/api/gear/parse", { raw_text: text }).then(function (data) {
    state.gearItems = data.items || [];
    _refreshGearUI();
    if (!state.gearItems.length) toast("未识别出装备，请检查输入（也可以先建计划之后再补）");
    var pending = state.gearItems.filter(function (g) { return g.needs_review; }).length;
    if (pending > 0) toast(pending + " 件装备未在装备库命中，请核对 AI 预估的参数后确认");
  }).catch(function (e) { toast("解析失败：" + e.message); })
    .finally(function () { btn.disabled = false; btn.textContent = "🔍 AI 识别装备参数"; });
}

function createPlan() {
  if (!state.route) return;
  var pending = (state.gearItems || []).filter(function (g) { return g.needs_review; }).length;
  if (pending > 0) { toast("还有 " + pending + " 件装备参数待确认"); return; }
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
    .finally(function () { _refreshGearUI(); });
}

// ── 视图 3：对账报告 ──────────────────────────────────

function reconcile(scenario) {
  if (!state.plan) { toast("请先创建或选择一个计划"); return; }
  var body = $("reportBody");
  // 已有报告在屏幕上时只做半透明提示，不整体隐藏（否则装备建议/补充装备栏会“消失”）
  var hasReport = !!state.report && !body.classList.contains("hidden");
  $("loading").classList.remove("hidden");
  $("loadingText").textContent = scenario === "normal"
    ? "正在重查沿线天气并与上次快照对比…"
    : "正在注入演示情景并重新对账…";
  if (hasReport) body.classList.add("reconciling");
  else body.classList.add("hidden");
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
      body.classList.remove("hidden");
      body.classList.remove("reconciling");
    });
}

function renderReport(r) {
  var route = state.routes.find(function (x) { return x.id === r.plan.route_id; }) || state.route || {};
  if (route.waypoints) state.route = route;   // 剖面图悬停采样依赖 state.route
  renderRouteProfile(route);
  applyRouteWx(route, r.plan.depart_date);    // 背景随线路天气：预报期内给预报，否则实时
  $("reportTitle").textContent = route.name || "对账报告";
  $("reportMeta").textContent = "出发 " + r.plan.depart_date +
    " · 装备 " + (r.plan.gear || []).length + " 件 · 对账于 " + fmtDate(r.reconciled_at) +
    (r.snapshot && r.snapshot.scenario !== "normal" ? " · 情景：" + r.snapshot.scenario : "");
  renderAlerts(r.events || [], r.gear_advice);
  renderGearAdvice(r.gear_advice);
  renderAgentReport(r);
  renderMatrix(r, route);
}

// 报告页：线路数据 + 海拔剖面 + 点位图例
function renderRouteProfile(route) {
  var box = $("routeProfile");
  if (!box) return;
  if (!route || !(route.waypoints || []).length) {
    box.innerHTML = '<p class="hint">未找到线路点位数据</p>';
    return;
  }
  // 剖面大图内部元素 id 固定（pfSvg 等），报告页渲染时清掉计划页的剖面，避免 id 重复
  var rs = $("routeSummary");
  if (rs) rs.innerHTML = "";
  state.pfPinned = false;
  var isTrail = route.activity === "trailrun";
  var risks = (route.waypoints || []).filter(function (p) { return p.risk; });
  box.innerHTML =
    '<p class="muted" style="font-size:12px">' + esc(route.region || "") + " · " + route.distance_km +
    " km · 爬升 " + route.ascent_m + " m · " + esc(route.difficulty || "") +
    (isTrail ? " · 🏃 越野跑" : " · 🥾 徒步 " + route.days + " 天") + "</p>" +
    profileChartSVG(route) +
    '<div class="pf-legend">' +
    '<span><i style="background:#FF9500"></i>垭口/山顶</span>' +
    '<span><i style="background:#007AFF"></i>营地</span>' +
    '<span><i style="background:#34C759"></i>' + (isTrail ? "CP 补给站" : "补给点") + "</span>" +
    '<span><i style="background:#8E8E93"></i>起终点/途经</span>' +
    "</div>" +
    (risks.length
      ? '<div class="pf-risks">' + risks.map(function (p) {
          return '<div class="pf-risk">⚠ Day ' + p.day + " " + esc(p.name) + "（" + p.elevation + "m）：" + esc(p.risk) + "</div>";
        }).join("") + "</div>"
      : "");
}

function renderGearAdvice(adv) {
  var box = $("gearAdvice");
  if (!box) return;
  if (!adv) {
    box.innerHTML = '<p class="hint">旧计划暂无建议数据，点“🔄 重查天气对账”刷新后生成</p>';
    return;
  }
  var html = [];
  var missing = adv.missing || [], adjust = adv.adjust || [],
      redundant = adv.redundant || [], ok = adv.ok || [];
  if (missing.length) {
    html.push('<div class="adv-group"><div class="adv-h adv-miss">🛒 需要准备 (' + missing.length + ')</div>');
    missing.forEach(function (m) {
      html.push('<div class="adv-item"><b>' + esc(m.name) + '</b><span>' + esc(m.reason) + '</span>' +
        (m.spec ? '<i class="adv-spec">📏 选购要点：' + esc(m.spec) + '</i>' : '') + '</div>');
    });
    html.push('</div>');
  }
  if (adjust.length) {
    html.push('<div class="adv-group"><div class="adv-h adv-adj">🔧 建议调整 (' + adjust.length + ')</div>');
    adjust.forEach(function (a) {
      html.push('<div class="adv-item"><b>' + esc(a.name) + '</b><span>' + esc(a.reason) +
        (a.suggestion ? ' — ' + esc(a.suggestion) : '') + '</span></div>');
    });
    html.push('</div>');
  }
  if (redundant.length) {
    html.push('<div class="adv-group"><div class="adv-h adv-cut">🪶 建议精简 (' + redundant.length + ')</div>');
    redundant.forEach(function (r) {
      html.push('<div class="adv-item"><b>' + esc(r.name) + '</b><span>' + esc(r.reason) + '</span></div>');
    });
    html.push('</div>');
  }
  if (ok.length) {
    // 兼容旧报告（字符串）与新格式（{name, spec, source}）：有复核要点的展开显示
    var okItems = ok.map(function (o) {
      return typeof o === "string" ? { name: o, spec: "" } : o;
    });
    var srcBadgeOf = function (o) {
      return o.source
        ? ' <span class="g-src g-src-' + esc(o.source) + '">' + (SOURCE_LABEL[o.source] || o.source) + "</span>"
        : "";
    };
    var withSpec = okItems.filter(function (o) { return o.spec; });
    var plain = okItems.filter(function (o) { return !o.spec; });
    html.push('<div class="adv-group"><div class="adv-h adv-ok">✅ 已就绪 (' + okItems.length + ')</div>');
    withSpec.forEach(function (o) {
      html.push('<div class="adv-item"><b>' + esc(o.name) + '</b>' + srcBadgeOf(o) +
        '<i class="adv-spec">📏 复核要点：' + esc(o.spec) + '</i></div>');
    });
    if (plain.length) {
      html.push('<div class="adv-chips">' + plain.map(function (o) {
        return '<span class="adv-chip">' + esc(o.name) + srcBadgeOf(o) + '</span>';
      }).join('') + '</div>');
    }
    html.push('</div>');
  }
  if (!html.length) html.push('<p class="hint">录入装备清单后，这里会给出针对性建议</p>');
  box.innerHTML = html.join("");
}

function addGearToPlan() {
  if (!state.plan) { toast("请先创建或选择一个计划"); return; }
  var input = $("gearAddInput");
  var text = (input.value || "").trim();
  if (!text) { toast("先输入要补充的装备"); input.focus(); return; }
  var btn = $("gearAddBtn");
  btn.disabled = true; btn.textContent = "解析中…";
  postJSON("/api/plans/" + state.plan.id + "/gear", { raw_text: text })
    .then(function (r) {
      state.plan = r.plan;
      var n = (r.added || []).length;
      if (!n) { toast("这些装备已在清单里，未重复添加"); return; }
      input.value = "";
      toast("已添加 " + (r.added.map(function (g) { return g.name; }).join("、")) + "，重新对账中…");
      reconcile("normal");   // 重跑对账，刷新装备建议与提醒
    })
    .catch(function (e) { toast("添加失败：" + e.message); })
    .finally(function () { btn.disabled = false; btn.textContent = "➕ 添加"; });
}

function renderAlerts(events, advice) {
  var box = $("alertList");
  if (!events.length) {
    // 天气风险无提醒，但装备清单可能仍有建议，文案要区分开，避免与下方卡片矛盾
    var pending = advice
      ? (advice.missing || []).length + (advice.adjust || []).length + (advice.redundant || []).length
      : 0;
    box.innerHTML = pending
      ? '<div class="no-alerts partial">✅ 天气风险对账通过 · 装备清单还有 ' + pending +
        ' 项建议，见下方「🎒 装备建议」</div>'
      : '<div class="no-alerts">✅ 天气与装备对账通过，暂无需要处理的提醒</div>';
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
      '<td><div class="wxm-wp">Day ' + (wp.day || "?") + " " + esc(wp.name || c.waypoint_id) + "</div>" +
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
      $("planList").innerHTML = '<p class="muted">还没有计划</p>' +
        '<button class="primary-btn" onclick="newPlanFromDrawer()">＋ 去对话新建计划</button>';
      return;
    }
    $("planList").innerHTML = plans.map(function (p) {
      return '<div class="plan-row" onclick="openPlan(\'' + esc(p.id) + '\')">' +
        '<div class="pr-main">' +
        '<div class="pr-name">' + esc(p.route_name) + "</div>" +
        '<div class="pr-meta">出发 ' + esc(p.depart_date) + " · 装备 " + (p.gear || []).length +
        " 件 · 快照 " + (p.snapshots || []).length + " 份</div></div>" +
        '<button class="pr-del" title="删除计划" onclick="event.stopPropagation();deletePlan(\'' + esc(p.id) + '\')">🗑</button>' +
        "</div>";
    }).join("");
  }).catch(function (e) { $("planList").innerHTML = '<p class="muted">加载失败：' + esc(e.message) + "</p>"; });
}

function newPlanFromDrawer() {
  closeDrawer();
  showView("chat");   // 新建计划入口：和 AI 聊，选线路 → 填日期/装备
  var input = $("chatInput");
  if (input) input.focus();
}

function deletePlan(planId) {
  if (!confirm("确定删除这个计划？关联的快照与报告会一并删除")) return;
  api("/api/plans/" + planId, { method: "DELETE" }).then(function () {
    toast("计划已删除");
    if (state.plan && state.plan.id === planId) { state.plan = null; state.report = null; }
    state.producedPlans = state.producedPlans.filter(function (p) { return p.plan_id !== planId; });
    _renderChatAside();
    showMyPlans();   // 刷新列表
  }).catch(function (e) { toast("删除失败：" + e.message); });
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

// ── 视图：AI 对话（阶段一）─────────────────────────────────

function autoGrow(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
}

function onChatKey(ev) {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    sendMessage();
  }
}

function quickAsk(text) {
  $("chatInput").value = text;
  autoGrow($("chatInput"));
  sendMessage();
}

function _scrollChatBottom() {
  var box = $("chatMessages");
  box.scrollTop = box.scrollHeight;
}

function _appendMsg(role, html) {
  var box = $("chatMessages");
  var div = document.createElement("div");
  div.className = "msg " + role;
  div.innerHTML = '<div class="msg-avatar">' + (role === "user" ? "🧑" : "🏔️") + "</div>" +
    '<div class="msg-bubble">' + html + "</div>";
  box.appendChild(div);
  _scrollChatBottom();
  return div;
}

function _appendToolBubble(label) {
  var box = $("chatMessages");
  var div = document.createElement("div");
  div.className = "tool-bubble";
  div.innerHTML = '<div class="spinner"></div><span><span class="ic">⚙</span> ' + esc(label) + "</span>";
  box.appendChild(div);
  _scrollChatBottom();
  return div;
}

function _finishToolBubble(el, brief) {
  el.classList.add("done");
  el.innerHTML = '<span class="ic">✓</span><span>' + esc(brief) + "</span>";
}

// 排队提示气泡：模型限流排队时的柔和提示（非报错样式），同一轮只更新不叠加
function _appendNoticeBubble(text) {
  var box = $("chatMessages");
  var div = document.createElement("div");
  div.className = "tool-bubble notice-bubble";
  div.innerHTML = '<div class="spinner"></div><span class="nb-text">' + esc(text) + "</span>";
  box.appendChild(div);
  _scrollChatBottom();
  return div;
}

function _ensureConversation() {
  if (state.conversation) return Promise.resolve(state.conversation);
  return postJSON("/api/chat/conversations", {}).then(function (c) {
    state.conversation = c;
    return c;
  });
}

function sendMessage() {
  if (state.chatBusy) return;
  var text = $("chatInput").value.trim();
  var attach = state.chatAttach.slice();
  if (!text && !attach.length) return;
  if (!state.meta.llm_enabled) {
    toast("对话功能需要配置 MODELSCOPE_API_KEY");
    return;
  }
  // 隐藏欢迎语
  var w = $("chatWelcome");
  if (w) w.classList.add("hidden");

  // 用户气泡：附件缩略 + 文字
  var thumbs = attach.map(function (a) {
    return a.kind === "image"
      ? '<img class="msg-img" src="' + a.dataUrl + '" alt="">'
      : '<span class="msg-file">🗺 ' + esc(a.name) + "</span>";
  }).join("");
  _appendMsg("user", (thumbs ? '<div class="msg-attach">' + thumbs + "</div>" : "") + esc(text));
  if (text) state.lastUserText = text;   // 主题引擎提取日期用

  $("chatInput").value = "";
  autoGrow($("chatInput"));
  state.chatAttach = [];
  _renderAttachPreview();
  state.chatBusy = true;
  $("sendBtn").disabled = true;
  $("sendBtn").textContent = "…";

  var images = attach.filter(function (a) { return a.kind === "image"; })
    .map(function (a) { return a.dataUrl; });
  var gpxes = attach.filter(function (a) { return a.kind === "gpx"; });

  _uploadGpxAll(gpxes).then(function (notes) {
    var fullText = notes.length ? (text ? text + "\n" : "") + notes.join("\n") : text;
    if (!fullText && images.length) fullText = "请看我发的图片。";
    return _ensureConversation().then(function (conv) {
      return _streamChat(conv.id, fullText, images);
    });
  }).catch(function (e) {
    _appendMsg("assistant", '<span style="color:var(--danger)">⚠ ' + esc(e.message) + "</span>");
  }).finally(function () {
    state.chatBusy = false;
    $("sendBtn").disabled = false;
    $("sendBtn").textContent = "发送";
  });
}

// 用 fetch + ReadableStream 解析 POST SSE（EventSource 不支持 POST）
function _streamChat(convId, text, images) {
  return fetch(API + "/api/chat/conversations/" + convId + "/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify({ text: text, images: images || [] }),
  }).then(function (res) {
    if (!res.ok) {
      return res.json().catch(function () { return {}; }).then(function (b) {
        throw new Error(b.detail || ("请求失败 " + res.status));
      });
    }
    var reader = res.body.getReader();
    var decoder = new TextDecoder("utf-8");
    var buffer = "";
    var assistantEl = null;       // 流式回复气泡
    var assistantHtml = "";       // 累计的 markdown 文本
    var pendingTool = null;       // 当前工具气泡
    var noticeEl = null;          // 排队提示气泡（模型限流时）

    function clearNotice() {
      if (noticeEl) { noticeEl.remove(); noticeEl = null; }
    }
    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) return;
        buffer += decoder.decode(chunk.value, { stream: true });
        // SSE 以 \n\n 分隔事件
        var parts = buffer.split("\n\n");
        buffer = parts.pop();      // 最后一段可能不完整，留到下次
        parts.forEach(function (part) { _handleSSE(part, ctx); });
        return pump();
      });
    }

    var ctx = {
      onToolStart: function (data) {
        clearNotice();
        pendingTool = _appendToolBubble(data.label);
      },
      onToolEnd: function (data) {
        if (pendingTool) { _finishToolBubble(pendingTool, data.result_brief); pendingTool = null; }
        else { _appendToolBubble(data.result_brief); }  // 兜底
        if (data.routes && data.routes.length) _appendRouteCards(data.routes);
      },
      onToken: function (data) {
        clearNotice();
        if (!assistantEl) {
          // 关掉残留的工具气泡（如有）
          if (pendingTool) { pendingTool.classList.add("done"); pendingTool = null; }
          assistantEl = _appendMsg("assistant", '<span class="cursor"></span>');
        }
        assistantHtml += data.text;
        assistantEl.querySelector(".msg-bubble").innerHTML = mdToHtml(assistantHtml) + '<span class="cursor"></span>';
        _scrollChatBottom();
      },
      onNotice: function (data) {
        // 排队中：同一条气泡原地更新文案，不刷屏
        if (noticeEl) noticeEl.querySelector(".nb-text").textContent = data.message || "";
        else noticeEl = _appendNoticeBubble(data.message || "");
        _scrollChatBottom();
      },
      onDone: function (data) {
        // 排队到最后也没挤进去时，收尾提示保留在对话里，只去掉转圈
        if (noticeEl) {
          var sp = noticeEl.querySelector(".spinner");
          if (sp) sp.remove();
          noticeEl.classList.add("done");
          noticeEl = null;
        }
        if (assistantEl) {
          assistantEl.querySelector(".msg-bubble").innerHTML = mdToHtml(assistantHtml);
        }
        if (data.plan_id) _addProducedPlan(data.plan_id);
      },
      onError: function (data) {
        clearNotice();
        _appendMsg("assistant", '<span style="color:var(--danger)">⚠ ' + esc(data.message || "出错了") + "</span>");
      },
    };

    return pump();
  });
}

// 工具结果里的线路 → 对话内线路卡片（点击进入创建计划）
function _appendRouteCards(routes) {
  var box = $("chatMessages");
  var div = document.createElement("div");
  div.className = "chat-route-cards";
  div.innerHTML = routes.map(function (r) {
    // search_routes 精简结果无 waypoints，用本地线路表补全后才能画剖面缩略图
    var full = state.routes.find(function (x) { return x.id === r.id; }) || r;
    var actTag = full.activity === "trailrun"
      ? '<span class="act-tag act-trailrun">🏃 越野跑</span>'
      : '<span class="act-tag act-hiking">🥾 徒步 ' + (full.days || "?") + ' 天</span>';
    return '<div class="route-card" onclick="selectRoute(\'' + esc(full.id) + '\')">' +
      '<div class="rc-top"><h3>' + esc(full.name) + "</h3>" + actTag + "</div>" +
      '<div class="rc-region">📍 ' + esc(full.region || "") + "</div>" +
      ((full.waypoints || []).length ? elevSVG(full) : "") +
      '<div class="rc-stats">' +
      "<div><b>" + full.distance_km + "</b>公里</div>" +
      "<div><b>" + full.ascent_m + "</b>累计爬升 m</div>" +
      "<div><b>" + esc(full.difficulty || "") + "</b>难度</div>" +
      "</div>" +
      '<span class="rc-go">选这条 · 填装备建计划 →</span>' +
      "</div>";
  }).join("");
  box.appendChild(div);
  _scrollChatBottom();
  // 聊到哪条线路，背景就切到那里的天气：用户提过日期且在预报期内则用预报
  var first = state.routes.find(function (x) { return x.id === routes[0].id; }) || routes[0];
  if ((first.waypoints || []).length) {
    applyRouteWx(first, _extractDateFromText(state.lastUserText), "chat");
  }
}

function _handleSSE(rawEvent, ctx) {
  // 解析 event: xxx \n data: {...}
  var evtType = "message", dataLines = [];
  rawEvent.split("\n").forEach(function (line) {
    if (line.indexOf("event:") === 0) evtType = line.slice(6).trim();
    else if (line.indexOf("data:") === 0) dataLines.push(line.slice(5).trim());
  });
  if (!dataLines.length) return;
  var data = {};
  try { data = JSON.parse(dataLines.join("\n")); } catch (e) { return; }
  if (evtType === "tool_start") ctx.onToolStart(data);
  else if (evtType === "tool_end") ctx.onToolEnd(data);
  else if (evtType === "token") ctx.onToken(data);
  else if (evtType === "notice") ctx.onNotice(data);
  else if (evtType === "done") ctx.onDone(data);
  else if (evtType === "error") ctx.onError(data);
}

function _addProducedPlan(planId) {
  // 拉计划详情，渲染到侧栏（done 事件每轮都携带 conv.plan_id，按 plan_id 去重/刷新）
  api("/api/plans/" + planId + "/report").then(function (r) {
    var info = {
      plan_id: planId,
      route_name: (state.routes.find(function (x) { return x.id === r.plan.route_id; }) || {}).name || "计划",
      depart_date: r.plan.depart_date,
      gear_count: (r.plan.gear || []).length,
      alert_count: (r.events || []).length,
      report: r,
    };
    var existed = state.producedPlans.find(function (p) { return p.plan_id === planId; });
    if (existed) {
      Object.assign(existed, info);   // 重查对账后刷新卡片数据，不重复添加
    } else {
      state.producedPlans.push(info);
    }
    _renderChatAside();
  }).catch(function () {});
}

function _renderChatAside() {
  var box = $("chatAsideBody");
  if (!state.producedPlans.length) {
    box.innerHTML = '<p class="chat-empty-aside">通过对话创建的计划会显示在这里。</p>';
    return;
  }
  box.innerHTML = state.producedPlans.map(function (p) {
    return '<div class="plan-card-mini" onclick=\'openProducedPlan("' + p.plan_id + '")\'>' +
      '<div class="pn">' + esc(p.route_name) + "</div>" +
      '<div class="pm">出发 ' + esc(p.depart_date) + " · 装备 " + p.gear_count + " 件 · " + p.alert_count + " 项提醒</div>" +
      '<span class="go">查看对账报告 →</span></div>';
  }).join("");
}

function openProducedPlan(planId) {
  var info = state.producedPlans.find(function (p) { return p.plan_id === planId; });
  if (!info) return;
  state.plan = info.report.plan;
  state.report = info.report;
  var route = state.routes.find(function (x) { return x.id === info.report.plan.route_id; });
  if (route) state.route = route;
  renderReport(info.report);
  showView("report");
  toast("已加载对话产出的计划，可点'重查天气对账'刷新");
}

// ── 视图 5：实时监测 Dashboard（阶段二）─────────────────────

var _liveChart = null;
var _liveData = null;          // {series:[...], hours}
var _liveCurrentWp = 0;
var _liveInterval = null;      // 轮询时间显示

function openLiveMonitor() {
  if (!state.plan) { toast("先创建或打开一个计划"); return; }
  if (state.meta.weather_source === "demo") {
    toast("实时监测需配置 TJ_API_KEY（真实天机气象数据）");
    return;
  }
  showView("live");
  var route = state.routes.find(function (x) { return x.id === state.plan.route_id; }) || state.route || {};
  $("liveTitle").textContent = "实时监测 · " + (route.name || "计划");
  $("liveMeta").textContent = "出发 " + state.plan.depart_date + " · " +
    ((route.waypoints || []).length) + " 个监测点";
  // 先停止旧连接
  stopLiveStream();
  loadLiveHourly();
}

function loadLiveHourly() {
  $("liveAlertList").innerHTML = '<div class="no-alerts">正在拉取沿线小时预报…</div>';
  api("/api/plans/" + state.plan.id + "/live/hourly?hours=48").then(function (data) {
    _liveData = data;
    _liveCurrentWp = 0;
    renderLiveTabs();
    renderLiveChart();
  }).catch(function (e) {
    $("liveAlertList").innerHTML = '<div class="no-alerts" style="color:var(--danger)">加载失败：' + esc(e.message) + "</div>";
  });
}

function renderLiveTabs() {
  var series = (_liveData && _liveData.series) || [];
  $("liveWpTabs").innerHTML = series.map(function (s, i) {
    return '<span class="live-wp-tab' + (i === _liveCurrentWp ? " active" : "") +
      '" onclick="switchLiveWp(' + i + ')">Day ' + s.day + " · " + esc(s.waypoint_name) +
      " (" + s.elevation + "m)</span>";
  }).join("");
}

function switchLiveWp(i) {
  _liveCurrentWp = i;
  renderLiveTabs();
  renderLiveChart();
}

function renderLiveChart() {
  if (!_liveData || !_liveData.series.length) return;
  var s = _liveData.series[_liveCurrentWp] || _liveData.series[0];
  var hours = s.hours || [];
  var labels = hours.map(function (h) { return h.datetime.slice(11, 16); });
  var datasets = [
    { label: "气温 °C", data: hours.map(function (h) { return h.t2m; }),
      borderColor: "#FF9500", backgroundColor: "rgba(255,149,0,.12)",
      yAxisID: "y", tension: 0.3, pointRadius: 0, borderWidth: 2, fill: true },
    { label: "降水 mm", data: hours.map(function (h) { return h.tp_mm; }),
      borderColor: "#0062CC", backgroundColor: "rgba(0,122,255,.25)",
      yAxisID: "y1", type: "bar", order: 2 },
    { label: "风速 m/s", data: hours.map(function (h) { return h.ws10m; }),
      borderColor: "#34C759", backgroundColor: "transparent",
      yAxisID: "y", tension: 0.3, pointRadius: 0, borderWidth: 1.5,
      borderDash: [4, 4] },
  ];
  if (_liveChart) _liveChart.destroy();
  var ctx = $("liveChart").getContext("2d");
  _liveChart = new Chart(ctx, {
    type: "line",
    data: { labels: labels, datasets: datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#8E8E93", font: { size: 11 }, boxWidth: 12 } },
        tooltip: { callbacks: { title: function (its) {
          return s.waypoint_name + " · " + hours[its[0].dataIndex].datetime.slice(5, 16);
        } } },
      },
      scales: {
        x: { ticks: { color: "#8E8E93", maxTicksLimit: 12, font: { size: 10 } },
          grid: { color: "rgba(60,60,67,.12)" } },
        y: { position: "left", ticks: { color: "#FF9500", font: { size: 10 } },
          grid: { color: "rgba(60,60,67,.12)" }, title: { display: true, text: "°C / m/s", color: "#8E8E93" } },
        y1: { position: "right", ticks: { color: "#0062CC", font: { size: 10 } },
          grid: { drawOnChartArea: false }, title: { display: true, text: "mm", color: "#8E8E93" } },
      },
    },
  });
}

function toggleLiveStream() {
  if (state.liveStream) stopLiveStream();
  else startLiveStream();
}

function startLiveStream() {
  if (!state.plan) return;
  postJSON("/api/plans/" + state.plan.id + "/live/start", {}).catch(function () {});
  // 危险告警需要系统级通知，提前申请权限
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }
  // EventSource 用 GET，恰好匹配 /live/stream
  var es = new EventSource(API + "/api/plans/" + state.plan.id + "/live/stream?interval=60");
  state.liveStream = es;
  $("liveToggleBtn").textContent = "⏸ 停止监测";
  $("liveDot").classList.add("live-on");
  $("liveStatusText").textContent = "监测中 · 每 60s 重查";
  var firstTick = true;
  es.addEventListener("tick", function (ev) {
    var data = JSON.parse(ev.data);
    _renderLiveAlerts(data.alerts || [], firstTick);
    _notifyDanger(data.alerts || []);
    $("liveStatusText").textContent = "监测中 · 最近一轮 " + (data.taken_at || "").slice(11, 19);
    firstTick = false;
  });
  es.addEventListener("error", function (ev) {
    // SSE 原生 error 事件无 data；服务端错误会通过 event:error 推送
  });
  es.addEventListener("error-msg", function () {});
  // 服务端 event:error 浏览器会归到 onerror，data 丢失；改用通用 message 兜底解析
  es.onmessage = function (ev) {
    try {
      var d = JSON.parse(ev.data);
      if (d.message) {
        $("liveStatusText").textContent = "查询出错：" + d.message.slice(0, 30);
      }
    } catch (e) {}
  };
}

function stopLiveStream() {
  if (state.liveStream) {
    state.liveStream.close();
    state.liveStream = null;
    if (state.plan) {
      postJSON("/api/plans/" + state.plan.id + "/live/stop", {}).catch(function () {});
    }
  }
  var btn = $("liveToggleBtn"); if (btn) btn.textContent = "▶ 开始监测";
  var dot = $("liveDot"); if (dot) dot.classList.remove("live-on");
  var st = $("liveStatusText"); if (st) st.textContent = "未开始";
}

function _notifyDanger(alerts) {
  // 高危告警同步推系统通知（浏览器在后台也能看到）
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  alerts.filter(function (a) { return a.severity === "danger"; })
    .slice(0, 3)
    .forEach(function (a) {
      try {
        new Notification("⛰ " + a.title, {
          body: a.detail + (a.suggestion ? "\n建议：" + a.suggestion : ""),
          tag: a.title + "|" + (a.datetime || ""),   // 同一告警不重复弹
        });
      } catch (e) {}
    });
}

function _renderLiveAlerts(alerts, isFirst) {
  var box = $("liveAlertList");
  if (isFirst && !alerts.length) {
    box.innerHTML = '<div class="no-alerts">✅ 暂无突变，持续监测中…</div>';
    return;
  }
  if (!alerts.length) return;   // 后续空轮不覆盖已有告警
  // 新告警插到最前
  var html = alerts.map(function (a) {
    return '<div class="live-alert-item ' + esc(a.severity) + '">' +
      '<div class="at">' + (SEV_ICON[a.severity] || "") + " " + esc(a.datetime || "") + " · " + esc(a.waypoint_name || "") + "</div>" +
      '<div class="at-t">' + esc(a.title) + "</div>" +
      '<div class="at-d">' + esc(a.detail) + (a.suggestion ? " 建议：" + esc(a.suggestion) : "") + "</div>" +
      "</div>";
  }).join("");
  // 保留最多 20 条
  var existing = box.querySelectorAll(".live-alert-item");
  if (existing.length > 20 - alerts.length) {
    for (var i = existing.length - 1; i >= 20 - alerts.length; i--) existing[i].remove();
  }
  var noAlert = box.querySelector(".no-alerts");
  if (noAlert) noAlert.remove();
  box.insertAdjacentHTML("afterbegin", html);
}

init();

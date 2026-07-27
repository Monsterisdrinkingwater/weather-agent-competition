"""装备建议引擎：结合线路、天气快照与已录装备，产出结构化建议清单。

三类输出（规划阶段核心交付物）：
- missing：按活动类型 + 天气条件推导的"需要准备"清单（用户清单里没有）
- adjust ：已有装备但参数与预报存在缺口，"建议调整"（复用 gear_gap 事件）
- redundant：与活动类型不匹配的装备（如越野跑带睡袋），"建议精简"
- ok     ：交叉检查通过的装备
"""
import re
from typing import Any, Dict, List

from models import AlertEvent, Plan, Route, WeatherSnapshot


def _conditions(route: Route, snap: WeatherSnapshot) -> Dict[str, Any]:
    """把快照压缩成建议规则用的全程条件概览。"""
    cells = snap.cells
    elev_max = max((w.elevation for w in route.waypoints), default=0)
    if not cells:
        # 预报窗口未覆盖/数据源无数据：哨兵值仅用于触发判断，不得进入文案
        return {"t_min": 99.0, "t_max": -99.0, "tp_max": 0.0,
                "ws_max": 0.0, "range_max": 0.0, "elev_max": elev_max,
                "no_data": True}
    return {
        "t_min": min(c.t_min for c in cells),
        "t_max": max(c.t_max for c in cells),
        "tp_max": max(c.tp_mm for c in cells),
        "ws_max": max(c.ws10m_max for c in cells),
        "range_max": max(c.t_max - c.t_min for c in cells),
        "elev_max": elev_max,
        "no_data": False,
    }


# 规则表：(适用活动, 触发条件fn, 装备名, 匹配已有装备的正则, 匹配类别, 理由模板fn, 选购要点fn)
# 活动: both / hiking / trailrun；类别为空时只按名称匹配（other 类太泛不能作匹配依据）
# 要点fn 把天气条件翻译成可量化的性能维度：保暖温标/防水静水压/透气量/抗风/齿深…
_RULES: List[tuple] = [
    ("hiking", lambda c: True, "睡袋",
     r"睡袋", "sleep",
     lambda c: ("多日线路必备，夜间营地降温明显" if c.get("no_data") else
                "多日线路必备，全程最低温 {:.1f}°C".format(c["t_min"])),
     lambda c: ("舒适温标按预报夜间最低温再留 3°C 余量；干冷选 650 蓬以上羽绒，"
                "潮湿环境选化纤棉或羽绒+防水外袋，配 R值≥3 睡垫才不漏冷"
                if c.get("no_data") else
                "舒适温标 ≤ {:.0f}°C（夜温再留 3°C 余量）；干冷选 650 蓬以上羽绒，"
                "潮湿环境选化纤棉或羽绒+防水外袋，配 R值≥3 睡垫才不漏冷".format(c["t_min"] - 3))),
    ("hiking", lambda c: True, "帐篷/庇护所",
     r"帐篷|天幕|庇护", "shelter",
     lambda c: ("多日野营必备，高山营地夜风不可小视" if c.get("no_data") else
                "多日野营必备，最大风速 {:.1f}m/s".format(c["ws_max"])),
     lambda c: ("抗风需覆盖 {:.0f}m/s：交叉双杆/四季帐 + 全部风绳打满；"
                "外帐防水 ≥3000mm、帐底 ≥5000mm、接缝压胶".format(c["ws_max"])
                if c["ws_max"] >= 10 else
                "双层三季帐即可；外帐防水 ≥3000mm、帐底 ≥5000mm、接缝压胶，风绳预留")),
    ("both", lambda c: True, "防水层（硬壳/雨披）",
     r"冲锋衣|硬壳|雨衣|雨披|防水", "rain",
     lambda c: ("最大日降水 {:.1f}mm，降雨+风是失温头号诱因".format(c["tp_max"])
                if c["tp_max"] >= 3 else "山区天气多变，防水层是强制装备"),
     lambda c: ("静水压 ≥20000mm、透气 ≥15000g/m²/24h，接缝全压胶、可调风帽、腕口魔术贴"
                if c["tp_max"] >= 10 or c["ws_max"] >= 10 else
                "静水压 ≥10000mm、透气 ≥8000g/m²/24h，接缝压胶、带风帽；"
                "只防水不透气的雨衣长时间行进会闷湿贴身")),
    ("both", lambda c: c["t_min"] <= 10, "保暖层（羽绒/抓绒）",
     r"羽绒|抓绒|棉服|保暖", "warm",
     lambda c: "全程最低温 {:.1f}°C，静止/夜间需要保暖层".format(c["t_min"]),
     lambda c: ("行进用透气抓绒、静止用羽绒：700 蓬以上、充绒 ≥150g，外层防风；"
                "汗湿后保暖不崩盘可选化纤棉" if c["t_min"] <= 0 else
                "200 克重抓绒或轻量羽绒（充绒 60–100g），重点是透气排汗不积汗")),
    ("hiking", lambda c: True, "贴身速干层",
     r"速干|快干|排汗|美利奴|羊毛内衣|压缩衣", "",
     lambda c: "贴身层决定排汗效率，湿衣服是失温第一步",
     lambda c: "速干化纤或美利奴羊毛（150–200g/m²），严禁纯棉；"
               "多日线带一套干的营地替换"),
    ("trailrun", lambda c: True, "贴身速干层",
     r"速干|快干|排汗|美利奴|羊毛内衣|压缩衣", "",
     lambda c: "贴身层决定排汗效率，湿衣服是失温第一步",
     lambda c: "速干化纤或轻量羊毛（≤150g/m²），严禁纯棉；长距离防磨接缝平车，"
               "乳贴/凡士林提前防护"),
    ("hiking", lambda c: True, "徒步鞋",
     r"登山鞋|徒步鞋|重装鞋|gtx|高帮鞋", "footwear",
     lambda c: "长线负重，鞋是第一安全装备",
     lambda c: ("中高帮防水膜（GTX 类）+ 深齿大底，护踝防泥水"
                if c["tp_max"] >= 3 or c["t_min"] <= 3 else
                "中帮徒步鞋，大底齿深 ≥4mm；干燥天透气比防水膜更重要")),
    ("trailrun", lambda c: True, "越野跑鞋",
     r"越野跑鞋|跑鞋|hoka|salomon|凯乐石.*fuga", "footwear",
     lambda c: "赛道长时间奔跑，鞋是成绩与安全的基础",
     lambda c: "齿深 ≥4mm、湿地防滑橡胶大底，透气快干鞋面；不建议防水膜（进水排不出），"
               "长距离选大半码防黑指甲"),
    ("both", lambda c: True, "头灯",
     r"头灯|headlamp", "",
     lambda c: "清晨/夜间行进与营地照明必备",
     lambda c: "≥300 流明、续航 ≥10h，带红光模式；防水 IPX4 以上，备电池单独防水包装"),
    ("both", lambda c: True, "救生毯/急救包",
     r"救生毯|急救|医疗", "",
     lambda c: "失温与外伤应急，体积重量可忽略",
     lambda c: "救生毯选加厚双面银/橙（可重复使用款更结实抗撕）；急救包含弹性绷带、"
               "碘伏棉片、防水火源与高糖应急食品"),
    ("hiking", lambda c: True, "登山杖",
     r"登山杖|手杖", "",
     lambda c: "上下坡省力护膝，湿滑路面增加支点",
     lambda c: "外锁三节铝杖更可靠，配泥托；长坡多的线路建议双杖"),
    ("both", lambda c: c["t_min"] <= 0, "冰爪",
     r"冰爪", "",
     lambda c: "最低温 {:.1f}°C，清晨结冰路段防滑".format(c["t_min"]),
     lambda c: "链式 8 齿以上、不锈钢齿，硬底鞋才挂得住；纯雪地可用轻量链爪，冰坂要卡式"),
    ("both", lambda c: c["t_min"] <= 3, "帽子+手套",
     r"手套|帽|头巾|buff", "",
     lambda c: "低温大风下头手散热最快，失温前哨",
     lambda c: ("防风防水手套（内抓绒外防水）+ 抓绒帽遮耳；备一副薄手套换着用"
                if c["tp_max"] >= 3 else "防风抓绒帽 + 触屏防风手套；buff 颈套一物多用")),
    ("both", lambda c: c["t_max"] >= 28 or c["elev_max"] >= 3000, "防晒（帽/墨镜/防晒霜）",
     r"防晒|墨镜|遮阳|太阳镜", "",
     lambda c: ("高海拔紫外线强烈" if c["elev_max"] >= 3000
                else "最高温 {:.1f}°C，暴晒风险高".format(c["t_max"])),
     lambda c: ("SPF50+ 防晒霜（2h 补一次）、UPF50+ 遮阳帽、偏光墨镜镜片 ≥3 级"
                if c["elev_max"] >= 3000 else
                "SPF50+ 防晒霜、UPF50+ 遮阳帽；长袖皮肤衣比裸露补防晒更可靠")),
    ("trailrun", lambda c: c["t_max"] >= 26, "电解质/盐丸",
     r"电解质|盐丸", "",
     lambda c: "高温长时间输出，只补水不补盐易抽筋/低钠",
     lambda c: "按钠 500–700mg/h 补充，搭配碳水胶更耐受；提前训练中测试肠胃耐受度"),
    ("trailrun", lambda c: True, "软水壶/水袋",
     r"水袋|软水壶|水壶", "",
     lambda c: "沿线补水点有限，按 500ml/h 估算携水",
     lambda c: "500ml 软水壶×2 起步，背心前仓取用不停车；高温段按补给点间距加到 1L+"),
    ("hiking", lambda c: c["t_min"] <= 0, "炉具+燃料",
     r"炉头|气罐|套锅|炉具", "",
     lambda c: "冰点以下热食热饮是核心保命手段",
     lambda c: "四季气（异丁烷比例高）低温才点得着，气罐睡袋里焐暖；套锅 ≥900ml，打火机+防风火柴双保险"),
]


def _owned(plan: Plan, pattern: str, cat: str) -> List[str]:
    """名称正则或装备类别命中即算拥有（型号名常不含关键词，如“始祖鸟BetaLT”）。"""
    rx = re.compile(pattern, re.IGNORECASE)
    return [g.name for g in plan.gear
            if rx.search(g.name) or (cat and g.category == cat)]


# 越野跑不过夜：宿营/炊事系统属于无效负重（强制装备以赛事组委会清单为准）
_TRAILRUN_REDUNDANT = re.compile(r"睡袋|帐篷|天幕|炉头|炉具|气罐|套锅|防潮垫|睡垫")


def _redundant_gear(plan: Plan) -> List[Dict[str, str]]:
    """与活动类型不匹配的装备：越野跑无需携带宿营系统。"""
    if plan.activity != "trailrun":
        return []
    out: List[Dict[str, str]] = []
    for g in plan.gear:
        if g.category in ("sleep", "shelter") or _TRAILRUN_REDUNDANT.search(g.name):
            out.append({"name": g.name,
                        "reason": "越野跑当日完赛不过夜，宿营装备是无效负重，"
                                  "建议精简；若赛事强制装备清单要求则以组委会为准"})
    return out


def build_gear_advice(plan: Plan, route: Route, snap: WeatherSnapshot,
                      events: List[AlertEvent]) -> Dict[str, Any]:
    cond = _conditions(route, snap)

    # 1) 需要准备：规则表逐条比对（命中的带选购要点，未命中的进 missing）
    missing: List[Dict[str, str]] = []
    matched_names: set = set()
    spec_by_name: Dict[str, str] = {}   # 已有装备对应的性能要求，回填到 ok 清单
    for act, need_fn, item, pattern, cat, reason_fn, spec_fn in _RULES:
        if act != "both" and act != plan.activity:
            continue
        if not need_fn(cond):
            continue
        owned = _owned(plan, pattern, cat)
        if owned:
            matched_names.update(owned)
            for n in owned:
                spec_by_name.setdefault(n, spec_fn(cond))
        else:
            missing.append({"name": item, "reason": reason_fn(cond),
                            "spec": spec_fn(cond)})

    # 2) 建议调整：直接消费 gear_gap 事件（睡袋温标/防水指数/帐篷抗风缺口）
    adjust: List[Dict[str, str]] = []
    seen = set()
    for e in events:
        if e.kind != "gear_gap" or not e.gear_affected:
            continue
        for name in e.gear_affected:
            key = name + "|" + e.title
            if key in seen:
                continue
            seen.add(key)
            adjust.append({"name": name, "reason": e.detail,
                           "suggestion": e.suggestion})

    # 3) 建议精简：活动类型不匹配的装备（越野跑带睡袋/帐篷等）
    redundant = _redundant_gear(plan)

    # 4) 已就绪：录入过且未被点名调整/精简的装备（附对应性能要求供复核）
    flagged = set(a["name"] for a in adjust) | set(r["name"] for r in redundant)
    ok = [{"name": g.name, "spec": spec_by_name.get(g.name, ""),
           "source": g.param_source}
          for g in plan.gear if g.name not in flagged]

    return {
        "conditions": {k: round(v, 1) if isinstance(v, float) else v
                       for k, v in cond.items()},
        "missing": missing,
        "adjust": adjust,
        "redundant": redundant,
        "ok": ok,
    }

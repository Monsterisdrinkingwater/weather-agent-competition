"""对话 Agent 的 function calling 工具集。

每个工具都是对现有后端能力的薄封装（零业务重写）：
  search_routes     → _load_preset_routes() + 关键词过滤
  get_route_detail  → _get_route(id)
  parse_gear_list   → parse_gear(raw_text)
  create_plan       → 创建计划 + 首份天气快照（复用 create_plan 核心逻辑）
  check_weather_now → _take_snapshot + run_reconcile（即时对账）

工具按"线路 → 装备 → 建计划 → 查天气"的认知顺序分组，LLM 自行决定调用时机。
执行结果统一为 dict，回灌给 LLM 继续推理。

设计说明：
- 用类持有运行时依赖（storage/路线加载/快照逻辑），避免全局状态污染；
- TOOLS 导出 OpenAI function schema 供 LLM 调用；
- run_tool 按工具名分发，捕获异常转成 {error} 字典，不让单个工具挂掉整个对话。
"""
import logging
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from models import Plan, Route, WeatherSnapshot
from modules.gear import parse_gear

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── OpenAI function schema（供 LLM 调用）──────────────────────────

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_routes",
            "description": "搜索/推荐徒步或越野跑线路。可按地区、活动类型、难度筛选。用户说'想去云南徒步'时调用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "description": "地区关键词，如'云南'、'北京'、'梅里雪山'，可选"},
                    "activity": {"type": "string", "enum": ["hiking", "trailrun", "all"],
                                 "description": "活动类型：hiking多日徒步 / trailrun越野跑 / all全部，默认all"},
                    "difficulty": {"type": "string", "enum": ["入门", "进阶", "高强度", "all"],
                                   "description": "难度筛选，默认all"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_route_detail",
            "description": "获取某条线路的完整详情：全部点位、海拔、风险标注、累计爬升等。用户选定线路或询问细节时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "route_id": {"type": "string", "description": "线路 id"},
                },
                "required": ["route_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_gear_list",
            "description": "解析用户口头描述的装备清单，返回结构化装备列表（含品牌型号、温标/防水等参数）。用户提到装备或问'带什么'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "raw_text": {"type": "string", "description": "用户原始装备描述，如'黑冰B700睡袋、始祖鸟Beta LT、登山杖'"},
                },
                "required": ["raw_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_plan",
            "description": "在用户确认线路、出发日期、装备后创建出行计划，自动生成首份天气快照和对账。信息齐全且用户同意时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "route_id": {"type": "string", "description": "线路 id"},
                    "depart_date": {"type": "string", "description": "出发日期 YYYY-MM-DD"},
                    "gear_raw_text": {"type": "string", "description": "装备清单原文（会自动解析）"},
                },
                "required": ["route_id", "depart_date", "gear_raw_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_weather_now",
            "description": "即时查询某计划的沿线天气并跑对账，返回提醒事件和报告。用户问'天气怎么样''要不要改期'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string", "description": "已创建的计划 id"},
                },
                "required": ["plan_id"],
            },
        },
    },
]


class ToolRuntime:
    """持有工具执行所需的运行时依赖（依赖注入，便于测试与隔离）。"""

    def __init__(self, load_routes_fn, get_route_fn, take_snapshot_fn,
                 reconcile_fn, build_report_fn, storage):
        self.load_routes = load_routes_fn          # () -> List[Route]
        self.get_route = get_route_fn              # (route_id) -> Route
        self.take_snapshot = take_snapshot_fn      # (plan, route, scenario) -> WeatherSnapshot
        self.reconcile = reconcile_fn              # (plan, route, prev, new) -> List[AlertEvent]
        self.build_report = build_report_fn        # (plan, route, snap, events, prev) -> dict
        self.storage = storage


# ── 工具实现（每个返回 dict 给 LLM）───────────────────────────────

def _tool_search_routes(rt: ToolRuntime, args: Dict[str, Any]) -> Dict[str, Any]:
    region = (args.get("region") or "").strip()
    activity = args.get("activity") or "all"
    difficulty = args.get("difficulty") or "all"

    routes = rt.load_routes()
    if activity != "all":
        routes = [r for r in routes if r.activity == activity]
    if difficulty != "all":
        routes = [r for r in routes if r.difficulty == difficulty]
    if region:
        # 同时匹配地区名和线路名（用户可能说线路名而非地区）
        kw = region.lower()
        routes = [r for r in routes
                  if kw in r.region.lower() or kw in r.name.lower()]

    return {
        "count": len(routes),
        "routes": [{
            "id": r.id, "name": r.name, "region": r.region,
            "activity": r.activity, "days": r.days,
            "distance_km": r.distance_km, "ascent_m": r.ascent_m,
            "difficulty": r.difficulty, "summary": r.summary,
        } for r in routes[:8]],   # 控制上下文长度
    }


def _tool_get_route_detail(rt: ToolRuntime, args: Dict[str, Any]) -> Dict[str, Any]:
    route = rt.get_route(args["route_id"])
    return route.model_dump()


def _tool_parse_gear(rt: ToolRuntime, args: Dict[str, Any]) -> Dict[str, Any]:
    items = parse_gear(args["raw_text"])
    return {
        "count": len(items),
        "items": [{
            "name": g.name, "category": g.category, "params": g.params,
            "confidence": g.confidence, "note": g.note,
        } for g in items],
    }


def _tool_create_plan(rt: ToolRuntime, args: Dict[str, Any]) -> Dict[str, Any]:
    """建计划 + 首份天气快照 + 对账。复用 main.create_plan 的核心逻辑。"""
    from modules.diff_engine import run_reconcile
    route = rt.get_route(args["route_id"])
    gear = parse_gear(args["gear_raw_text"])
    plan = Plan(
        id=uuid.uuid4().hex[:10], route_id=route.id, activity=route.activity,
        depart_date=args["depart_date"], gear=[g.model_dump() for g in gear],
        created_at=_now(), status="planning",
    )
    rt.storage.put("plans", plan.id, plan.model_dump())
    snap = rt.take_snapshot(plan, route, "normal")
    events = run_reconcile(plan, route, None, snap)
    report = rt.build_report(plan, route, snap, events, None)
    result = {
        "plan": plan.model_dump(), "snapshot": snap.model_dump(),
        "events": [e.model_dump() for e in events], **report,
        "reconciled_at": _now(), "has_diff": False,
    }
    rt.storage.put("reports", plan.id, result)
    return {
        "plan_id": plan.id,
        "route_name": route.name,
        "depart_date": plan.depart_date,
        "gear_count": len(gear),
        "alert_count": len(events),
        "danger_count": sum(1 for e in events if e.severity == "danger"),
        "report_generated_by": report.get("generated_by"),
        "summary": "计划已创建，首份天气快照与对账报告已生成。",
    }


def _tool_check_weather(rt: ToolRuntime, args: Dict[str, Any]) -> Dict[str, Any]:
    """即时对账：重查天气 + diff + 报告。"""
    from models import AlertEvent
    plan_data = rt.storage.get("plans", args["plan_id"])
    if not plan_data:
        return {"error": "计划不存在: " + args["plan_id"]}
    plan = Plan(**plan_data)
    route = rt.get_route(plan.route_id)

    prev_snap = None
    if plan.snapshots:
        prev_data = rt.storage.get("snapshots", plan.snapshots[-1])
        if prev_data:
            prev_snap = WeatherSnapshot(**prev_data)

    new_snap = rt.take_snapshot(plan, route, "normal")
    events = rt.reconcile(plan, route, prev_snap, new_snap)
    return {
        "plan_id": plan.id,
        "snapshot_taken_at": new_snap.taken_at,
        "has_diff": prev_snap is not None,
        "alert_count": len(events),
        "danger_count": sum(1 for e in events if e.severity == "danger"),
        "events": [{
            "title": e.title, "severity": e.severity, "date": e.date,
            "waypoint_name": e.waypoint_name, "detail": e.detail,
            "suggestion": e.suggestion,
        } for e in events[:6]],
    }


_TOOL_IMPL = {
    "search_routes": _tool_search_routes,
    "get_route_detail": _tool_get_route_detail,
    "parse_gear_list": _tool_parse_gear,
    "create_plan": _tool_create_plan,
    "check_weather_now": _tool_check_weather,
}


def run_tool(rt: ToolRuntime, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """分发执行工具，异常转 {error} 不外抛。"""
    fn = _TOOL_IMPL.get(name)
    if not fn:
        return {"error": "未知工具: " + name}
    try:
        return fn(rt, args)
    except Exception as e:
        logger.warning("工具 %s 执行失败: %s: %s", name, type(e).__name__, str(e)[:200])
        return {"error": "{}: {}".format(type(e).__name__, str(e)[:300])}


def tool_label(name: str, args: Dict[str, Any]) -> str:
    """生成工具调用的人类可读标签（前端'正在做什么'气泡用）。"""
    if name == "search_routes":
        kw = args.get("region") or args.get("activity") or "线路"
        return "🔍 正在搜索" + kw + "相关线路"
    if name == "get_route_detail":
        return "📍 正在调取线路详情"
    if name == "parse_gear_list":
        return "🎒 正在识别装备参数"
    if name == "create_plan":
        return "📝 正在创建出行计划"
    if name == "check_weather_now":
        return "🌦 正在查询沿线天气"
    return "⚙ 正在处理"

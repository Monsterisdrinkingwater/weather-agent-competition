"""户外计划助手 — FastAPI 入口
API:
  GET  /api/routes                    线路库
  GET  /api/routes/{id}               线路详情
  POST /api/routes/gpx                GPX 导入
  POST /api/gear/parse                装备清单解析
  POST /api/plans                     创建计划（自动打首份天气快照）
  GET  /api/plans                     计划列表
  GET  /api/plans/{id}                计划详情
  POST /api/plans/{id}/reconcile      重查天气 → diff → 提醒事件 + Agent 报告
  GET  /api/plans/{id}/report         最近一次对账结果
  GET  /api/meta                      运行环境信息（演示/真实数据源等）
"""
import json
import logging
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import storage
from config import ROUTES_DIR, WEATHER_DEMO_MODE, MODELSCOPE_API_KEY, TAVILY_API_KEY
from models import Conversation, Plan, Route, WeatherSnapshot
from modules import gpx as gpx_mod
from modules.agent import build_report
from modules.agent_tools import ToolRuntime
from modules.chat_agent import run_chat
from modules.diff_engine import run_reconcile
from modules.gear import parse_gear
from modules.weather import get_source

app = FastAPI(title="户外计划助手", version="0.1.0")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── 线路 ────────────────────────────────────────────────────────

def _load_preset_routes() -> List[Route]:
    routes = []
    for p in sorted(ROUTES_DIR.glob("*.json")):
        routes.append(Route(**json.loads(p.read_text(encoding="utf-8"))))
    return routes


def _get_route(route_id: str) -> Route:
    for r in _load_preset_routes():
        if r.id == route_id:
            return r
    saved = storage.get("gpx_routes", route_id)
    if saved:
        return Route(**saved)
    raise HTTPException(404, "线路不存在: " + route_id)


@app.get("/api/routes")
def list_routes():
    preset = [r.model_dump() for r in _load_preset_routes()]
    imported = storage.all_values("gpx_routes")
    return {"routes": preset + imported}


@app.get("/api/routes/{route_id}")
def route_detail(route_id: str):
    return _get_route(route_id).model_dump()


@app.post("/api/routes/gpx")
async def import_gpx(file: UploadFile = File(...), name: str = Form("我的线路"),
                     activity: str = Form("hiking"), days: int = Form(3)):
    try:
        xml_text = (await file.read()).decode("utf-8", errors="ignore")
        route = gpx_mod.gpx_to_route(xml_text, name, activity, days)
    except Exception as e:
        raise HTTPException(400, "GPX 解析失败: {}".format(e))
    storage.put("gpx_routes", route.id, route.model_dump())
    return route.model_dump()


# ── 装备 ────────────────────────────────────────────────────────

class GearParseIn(BaseModel):
    raw_text: str


@app.post("/api/gear/parse")
def gear_parse(body: GearParseIn):
    items = parse_gear(body.raw_text)
    return {"items": [i.model_dump() for i in items]}


# ── 计划与对账 ──────────────────────────────────────────────────

class PlanIn(BaseModel):
    route_id: str
    depart_date: str
    gear_raw_text: str = ""
    gear_items: Optional[List[dict]] = None   # 前端已解析确认过的装备


def _take_snapshot(plan: Plan, route: Route, scenario: str) -> WeatherSnapshot:
    source = get_source()
    depart = date.fromisoformat(plan.depart_date)
    try:
        cells = source.fetch(route, depart, scenario)
    except Exception as e:
        raise HTTPException(502, "天气数据获取失败: {}".format(e))
    snap = WeatherSnapshot(
        id=uuid.uuid4().hex[:10], plan_id=plan.id, taken_at=_now(),
        source=source.name, scenario=scenario, cells=cells,
    )
    storage.put("snapshots", snap.id, snap.model_dump())
    plan.snapshots.append(snap.id)
    storage.put("plans", plan.id, plan.model_dump())
    return snap


@app.post("/api/plans")
def create_plan(body: PlanIn):
    route = _get_route(body.route_id)
    if body.gear_items is not None:
        gear = body.gear_items
    else:
        gear = [g.model_dump() for g in parse_gear(body.gear_raw_text)]
    plan = Plan(
        id=uuid.uuid4().hex[:10], route_id=route.id, activity=route.activity,
        depart_date=body.depart_date, gear=gear, created_at=_now(),
    )
    storage.put("plans", plan.id, plan.model_dump())
    snap = _take_snapshot(plan, route, "normal")
    # 首份快照也跑一次装备缺口检查（无 diff）
    events = run_reconcile(plan, route, None, snap)
    report = build_report(plan, route, snap, events, None)
    result = {
        "plan": plan.model_dump(), "snapshot": snap.model_dump(),
        "events": [e.model_dump() for e in events], **report,
        "reconciled_at": _now(), "has_diff": False,
    }
    storage.put("reports", plan.id, result)
    return result


@app.get("/api/plans")
def list_plans():
    plans = storage.all_values("plans")
    routes = {r["id"]: r for r in
              [x.model_dump() for x in _load_preset_routes()] + storage.all_values("gpx_routes")}
    for p in plans:
        r = routes.get(p["route_id"], {})
        p["route_name"] = r.get("name", "?")
        p["route_days"] = r.get("days", 0)
    return {"plans": sorted(plans, key=lambda p: p.get("created_at", ""), reverse=True)}


@app.get("/api/plans/{plan_id}")
def plan_detail(plan_id: str):
    p = storage.get("plans", plan_id)
    if not p:
        raise HTTPException(404, "计划不存在")
    return p


class ReconcileIn(BaseModel):
    scenario: str = "normal"   # 演示模式可传 coldwave / rainstorm 模拟天气突变


@app.post("/api/plans/{plan_id}/reconcile")
def reconcile(plan_id: str, body: ReconcileIn):
    p = storage.get("plans", plan_id)
    if not p:
        raise HTTPException(404, "计划不存在")
    plan = Plan(**p)
    route = _get_route(plan.route_id)

    prev_snap = None
    if plan.snapshots:
        prev_data = storage.get("snapshots", plan.snapshots[-1])
        if prev_data:
            prev_snap = WeatherSnapshot(**prev_data)

    new_snap = _take_snapshot(plan, route, body.scenario)
    events = run_reconcile(plan, route, prev_snap, new_snap)
    report = build_report(plan, route, new_snap, events, prev_snap)
    result = {
        "plan": plan.model_dump(),
        "snapshot": new_snap.model_dump(),
        "prev_snapshot": prev_snap.model_dump() if prev_snap else None,
        "events": [e.model_dump() for e in events], **report,
        "reconciled_at": _now(), "has_diff": prev_snap is not None,
    }
    storage.put("reports", plan.id, result)
    return result


@app.get("/api/plans/{plan_id}/report")
def last_report(plan_id: str):
    r = storage.get("reports", plan_id)
    if not r:
        raise HTTPException(404, "尚无对账记录")
    return r


@app.get("/api/meta")
def meta():
    return {
        "weather_source": "demo" if WEATHER_DEMO_MODE else "tjweather",
        "llm_enabled": bool(MODELSCOPE_API_KEY),
        "web_search_enabled": bool(TAVILY_API_KEY),
    }


# ── 对话式 Agent（阶段一）────────────────────────────────────────

def _tool_runtime() -> ToolRuntime:
    """构造工具运行时（依赖现有 main 内函数，零业务重写）。"""
    return ToolRuntime(
        load_routes_fn=_load_preset_routes,
        get_route_fn=_get_route,
        take_snapshot_fn=_take_snapshot,
        reconcile_fn=run_reconcile,
        build_report_fn=build_report,
        storage=storage,
    )


class ConversationIn(BaseModel):
    title: str = "新对话"
    route_id: Optional[str] = None


@app.post("/api/chat/conversations")
def create_conversation(body: ConversationIn):
    conv = Conversation(
        id=uuid.uuid4().hex[:10], title=body.title, route_id=body.route_id,
        created_at=_now(), updated_at=_now(),
    )
    storage.put("conversations", conv.id, conv.model_dump())
    return conv.model_dump()


@app.get("/api/chat/conversations")
def list_conversations():
    convs = storage.all_values("conversations")
    convs.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return {"conversations": convs}


@app.get("/api/chat/conversations/{conv_id}")
def get_conversation(conv_id: str):
    c = storage.get("conversations", conv_id)
    if not c:
        raise HTTPException(404, "对话不存在")
    conv = Conversation(**c)
    messages = []
    for mid in conv.messages:
        m = storage.get("messages", mid)
        if m:
            messages.append(m)
    return {**conv.model_dump(), "message_list": messages}


class ChatIn(BaseModel):
    text: str


@app.post("/api/chat/conversations/{conv_id}/messages")
def post_message(conv_id: str, body: ChatIn):
    """发消息 → SSE 流式返回（tool_start/tool_end/token/done/error）。"""
    c = storage.get("conversations", conv_id)
    if not c:
        raise HTTPException(404, "对话不存在")
    conv = Conversation(**c)
    rt = _tool_runtime()

    def event_stream():
        # yield from 不适合（要捕获 done 后清理），手动迭代
        for chunk in run_chat(storage, conv, body.text, rt):
            yield chunk

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 实时监测 Dashboard（阶段二）──────────────────────────────────

def _require_real_weather():
    """实时监测依赖真实小时级数据，demo 模式下不可用。"""
    if WEATHER_DEMO_MODE:
        raise HTTPException(400, "实时监测需配置 TJ_API_KEY 使用真实天机气象数据")


@app.get("/api/plans/{plan_id}/live/hourly")
def live_hourly(plan_id: str, hours: int = 48):
    """一次性取沿线各点位小时级预报（给时序图表用，默认未来 48h）。"""
    _require_real_weather()
    p = storage.get("plans", plan_id)
    if not p:
        raise HTTPException(404, "计划不存在")
    plan = Plan(**p)
    route = _get_route(plan.route_id)
    hours = max(6, min(hours, 72))   # 限制 6~72h，控制 API 调用量
    source = get_source()
    series = []
    for wp in route.waypoints:
        try:
            cells = source.fetch_hourly(wp, hours)
        except Exception as e:
            raise HTTPException(502, "天机气象查询失败: {}".format(e))
        series.append({
            "waypoint_id": wp.id, "waypoint_name": wp.name,
            "kind": wp.kind, "elevation": wp.elevation,
            "day": wp.day, "hours": [h.model_dump() for h in cells],
        })
    return {"plan_id": plan_id, "hours": hours, "series": series,
            "taken_at": _now()}


@app.post("/api/plans/{plan_id}/live/start")
def live_start(plan_id: str):
    """标记计划进入'活动进行中'，启停由前端控制 SSE 连接。"""
    p = storage.get("plans", plan_id)
    if not p:
        raise HTTPException(404, "计划不存在")
    p["status"] = "active"
    storage.put("plans", plan_id, p)
    return {"plan_id": plan_id, "status": "active"}


@app.post("/api/plans/{plan_id}/live/stop")
def live_stop(plan_id: str):
    """标记计划结束活动。"""
    p = storage.get("plans", plan_id)
    if not p:
        raise HTTPException(404, "计划不存在")
    p["status"] = "completed"
    storage.put("plans", plan_id, p)
    return {"plan_id": plan_id, "status": "completed"}


@app.get("/api/plans/{plan_id}/live/stream")
def live_stream(plan_id: str, interval: int = 60):
    """实时监测 SSE：每 interval 秒重查各点位小时预报，
    检测近 3h 内的天气突变（温降/大风/降水），推送 AlertEvent。

    前端保持连接即监测，断开即停止——服务端无状态轮询负担。
    """
    _require_real_weather()
    p = storage.get("plans", plan_id)
    if not p:
        raise HTTPException(404, "计划不存在")
    plan = Plan(**p)
    route = _get_route(plan.route_id)
    interval = max(30, min(interval, 600))   # 30s~10min

    def event_stream():
        import time as _time
        from modules.weather import get_source as _gs
        prev_hours: dict = {}    # waypoint_id -> 上一轮的小时数据 dict
        while True:
            source = _gs()
            alerts = []
            try:
                for wp in route.waypoints:
                    cells = source.fetch_hourly(wp, 6)   # 只查近 6h，省额度
                    for h in cells[-3:]:   # 看最近 3 个时刻
                        key = wp.id + "|" + h.datetime
                        prev = prev_hours.get(key)
                        if prev:
                            drop = prev.t2m - h.t2m
                            if drop >= 5:
                                alerts.append({
                                    "severity": "warning" if drop < 8 else "danger",
                                    "title": "{} 气温骤降".format(wp.name),
                                    "detail": "{} 气温 {:.1f}→{:.1f}°C（{}h 内降 {:.1f}°C）".format(
                                        h.datetime, prev.t2m, h.t2m, "3", drop),
                                    "waypoint_name": wp.name, "datetime": h.datetime,
                                    "suggestion": "注意保暖，必要时就地避风",
                                })
                            if h.ws10m_max - prev.ws10m_max >= 5 or h.ws10m >= 13.9:
                                alerts.append({
                                    "severity": "danger" if h.ws10m >= 13.9 else "warning",
                                    "title": "{} 大风预警".format(wp.name),
                                    "detail": "{} 风速达 {:.1f}m/s".format(
                                        h.datetime, h.ws10m),
                                    "waypoint_name": wp.name, "datetime": h.datetime,
                                    "suggestion": "避开垭口/山脊暴露段",
                                })
                        prev_hours[key] = h
                # 截断 prev_hours，避免无限增长（只保留最近一轮）
                prev_hours = {k: v for k, v in list(prev_hours.items())[-200:]}
            except Exception as e:
                yield "event: error\ndata: " + \
                    json.dumps({"message": "查询失败: {}".format(str(e)[:120])},
                               ensure_ascii=False) + "\n\n"

            payload = {
                "taken_at": _now(),
                "alert_count": len(alerts),
                "alerts": alerts[:10],
            }
            yield "event: tick\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            _time.sleep(interval)

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 前端静态资源 ────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

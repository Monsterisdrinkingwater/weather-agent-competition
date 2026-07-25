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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import storage
from config import ROUTES_DIR, WEATHER_DEMO_MODE, MODELSCOPE_API_KEY, TAVILY_API_KEY
from models import Plan, Route, WeatherSnapshot
from modules import gpx as gpx_mod
from modules.agent import build_report
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


# ── 前端静态资源 ────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

"""核心数据模型：Route / Plan / GearItem / WeatherSnapshot / AlertEvent"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class Waypoint(BaseModel):
    id: str
    name: str
    lat: float
    lon: float
    elevation: int                      # 海拔（米）
    kind: str                           # start / pass / camp / peak / water / finish / aid
    day: int = 1                        # 计划第几天到达（越野跑均为 1）
    risk: str = ""                      # 风险标注，如"垭口风大，失温高发"


class Route(BaseModel):
    id: str
    name: str
    activity: str                       # hiking / trailrun
    region: str
    days: int
    distance_km: float
    ascent_m: int                       # 累计爬升
    difficulty: str                     # 入门 / 进阶 / 高强度
    summary: str
    waypoints: List[Waypoint]
    source: str = "preset"              # preset / gpx


class GearItem(BaseModel):
    name: str                           # 用户原始输入，如"黑冰 B700"
    category: str                       # sleep / shelter / rain / warm / footwear / other
    params: Dict[str, Any] = Field(default_factory=dict)   # 温标/防水/克重等
    param_source: str = "unknown"       # web_search / llm_estimate / builtin / unknown
    confidence: str = "low"             # high / medium / low
    note: str = ""


class DayPointWeather(BaseModel):
    """某 waypoint 某天的天气（要素与天机首开 6 要素对齐）"""
    waypoint_id: str
    date: str                           # YYYY-MM-DD
    t_min: float                        # 由 t2m 序列聚合
    t_max: float
    ws10m_max: float                    # 当日最大 10m 风速 m/s
    wd10m: str                          # 主导风向
    rh2m_avg: float                     # 平均相对湿度 %
    tp_mm: float                        # 日降水量 mm
    slp_trend: str = "steady"           # rising / falling / steady


class WeatherSnapshot(BaseModel):
    id: str
    plan_id: str
    taken_at: str                       # ISO 时间
    source: str                         # tjweather / demo
    scenario: str = "normal"            # 演示模式下的情景标签
    cells: List[DayPointWeather]


class AlertEvent(BaseModel):
    """提醒事件——通知抽象层的统一载体。
    页面内对账、模拟推送、未来的邮件/推送通道都只消费这个结构。"""
    id: str
    plan_id: str
    created_at: str
    kind: str                           # temp_drop / precip_new / wind_jump / gear_gap / danger
    severity: str                       # info / warning / danger
    waypoint_id: str
    waypoint_name: str
    date: str
    title: str                          # 一句话结论
    detail: str                         # 变化前后数值
    gear_affected: List[str] = Field(default_factory=list)
    suggestion: str = ""                # 建议动作


class Plan(BaseModel):
    id: str
    route_id: str
    activity: str
    depart_date: str                    # YYYY-MM-DD
    gear: List[GearItem] = Field(default_factory=list)
    snapshots: List[str] = Field(default_factory=list)   # snapshot id 列表（时序）
    created_at: str = ""

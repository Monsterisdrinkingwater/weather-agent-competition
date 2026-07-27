"""天气数据源：
- TianjiSource：天机 API 逐点查询（拿到 Key 后自动启用）
- DemoSource：确定性模拟数据，随海拔/纬度/日期变化，支持情景注入

两者输出统一为 DayPointWeather 列表，上层不感知来源。
"""
import hashlib
import math
from datetime import date, timedelta
from typing import List

import httpx

from config import TJ_API_KEY, TJ_API_BASE, WEATHER_DEMO_MODE
from models import Route, DayPointWeather, HourWeather, Waypoint

_WIND_DIRS = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]


# ──────────────────────────── 演示数据源 ────────────────────────────

def _hnoise(*parts, lo: float = -1.0, hi: float = 1.0) -> float:
    """基于 md5 的确定性伪随机：同输入永远同输出，保证快照可复现。"""
    h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
    frac = int(h[:8], 16) / 0xFFFFFFFF
    return lo + frac * (hi - lo)


def _season_base_temp(d: date, lat: float) -> float:
    """粗略季节基准温度：7 月峰值，1 月谷值，纬度越高越冷。"""
    day_of_year = d.timetuple().tm_yday
    seasonal = 14.0 * math.cos((day_of_year - 197) / 365.0 * 2 * math.pi)
    return 22.0 + seasonal - (lat - 30.0) * 0.7


class DemoSource:
    """确定性模拟：scenario 用于演示天气突变。
    - normal：初次查询的基准天气
    - coldwave：寒潮南下，最低温骤降 6-9°C，风力+5m/s
    - rainstorm：降水系统过境，日降水 +15-30mm，湿度拉满
    """

    name = "demo"

    def fetch(self, route: Route, depart: date, scenario: str = "normal") -> List[DayPointWeather]:
        cells = []
        for wp in route.waypoints:
            d = depart + timedelta(days=wp.day - 1)
            base = _season_base_temp(d, wp.lat)
            # 海拔递减率 0.6°C / 100m
            alt_adj = -wp.elevation * 0.006
            jitter = _hnoise(route.id, wp.id, d.isoformat(), "t", lo=-2, hi=2)
            t_mid = base + alt_adj + jitter
            spread = 5.5 + _hnoise(wp.id, d, "spread", lo=0, hi=3)
            t_min = round(t_mid - spread, 1)
            t_max = round(t_mid + spread, 1)

            wind = 3.0 + _hnoise(wp.id, d, "w", lo=0, hi=5)
            if wp.kind in ("pass", "peak"):
                wind += 4.0 + _hnoise(wp.id, "gust", lo=0, hi=3)  # 垭口/山顶放大
            rain_roll = _hnoise(route.id, wp.id, d, "rain", lo=0, hi=1)
            tp = round(max(0.0, (rain_roll - 0.55)) * 40, 1)  # 45% 概率有雨
            rh = round(55 + rain_roll * 35 + _hnoise(wp.id, d, "rh", lo=-5, hi=5), 0)

            if scenario == "coldwave":
                drop = 6.0 + _hnoise(wp.id, d, "cw", lo=0, hi=3)
                t_min = round(t_min - drop, 1)
                t_max = round(t_max - drop * 0.7, 1)
                wind += 5.0
                slp_trend = "rising"
            elif scenario == "rainstorm":
                tp = round(tp + 15 + _hnoise(wp.id, d, "rs", lo=0, hi=15), 1)
                rh = min(98, rh + 20)
                t_max = round(t_max - 3.0, 1)
                slp_trend = "falling"
            else:
                slp_trend = "steady"

            wd = _WIND_DIRS[int(_hnoise(wp.id, d, "wd", lo=0, hi=7.99))]
            cells.append(DayPointWeather(
                waypoint_id=wp.id, date=d.isoformat(),
                t_min=t_min, t_max=t_max,
                ws10m_max=round(wind, 1), wd10m=wd,
                rh2m_avg=rh, tp_mm=tp, slp_trend=slp_trend,
            ))
        return cells


# ──────────────────────────── 天机真实数据源 ────────────────────────────

class TianjiSource:
    """天机 API 单点查询封装。
    接口文档：https://www.tjweather.com/info/doc/download/api/weather/json.html
    路径 /beta，参数 key/loc/fields/t_res/fcst_days/fcst_hours/grid/tz。
    """

    name = "tjweather"

    # 查询的要素（免费版 6 个地面要素）
    _FIELDS = "t2m,ws10m,wd10m,slp,rh2m,tp"

    def __init__(self):
        self.client = httpx.Client(base_url=TJ_API_BASE.rstrip("/"), timeout=15)

    def _request(self, lat: float, lon: float, fcst_days: int = 0,
                 fcst_hours: int = 0) -> dict:
        """查询单点预报。
        t_res=15min 提供最高时间精度，diff 引擎按天聚合不受影响，
        Chat Agent 可用 15min 数据回答"具体几点下雨"类问题。
        """
        resp = self.client.get(
            "/beta",
            params={
                "key": TJ_API_KEY,
                "loc": f"{lon},{lat}",           # 经度在前，纬度在后
                "fields": self._FIELDS,
                "fcst_days": str(fcst_days),
                "fcst_hours": str(fcst_hours),
                "t_res": "15min",                 # 最高时间精度
                "tz": "8",                        # 北京时间
                "grid": "1",                      # 最高空间精度(2.5km)
            },
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _aggregate_day(hourly: List[dict], waypoint_id: str, day: str) -> DayPointWeather:
        """逐小时 → 日聚合。hourly: [{t2m, ws10m, wd10m, slp, rh2m, tp}, ...]"""
        t = [h["t2m"] for h in hourly]
        ws = [h["ws10m"] for h in hourly]
        rh = [h["rh2m"] for h in hourly]
        tp = sum(h.get("tp", 0) for h in hourly)
        slp_first, slp_last = hourly[0].get("slp", 0), hourly[-1].get("slp", 0)
        trend = "rising" if slp_last - slp_first > 1.5 else (
            "falling" if slp_first - slp_last > 1.5 else "steady")
        # 主导风向取风速最大时刻
        wd = max(hourly, key=lambda h: h["ws10m"]).get("wd10m", "")
        return DayPointWeather(
            waypoint_id=waypoint_id, date=day,
            t_min=round(min(t), 1), t_max=round(max(t), 1),
            ws10m_max=round(max(ws), 1), wd10m=str(wd),
            rh2m_avg=round(sum(rh) / len(rh), 0), tp_mm=round(tp, 1),
            slp_trend=trend,
        )

    @staticmethod
    def _extract_series(resp: dict) -> List[dict]:
        """从天机 /beta 响应里取出逐时序列。

        文档结构：{code, message, data: {units, data: [{time, t2m, ...}], time_init}}
        兼容旧字段 hourly / data 两种放法。
        """
        body = resp.get("data", resp)
        series = body.get("data") or body.get("hourly") or []
        return series if isinstance(series, list) else []

    def fetch(self, route: Route, depart: date, scenario: str = "normal") -> List[DayPointWeather]:
        cells = []
        # fcst_days 至少覆盖到行程最后一天（文档：tot_hrs = fcst_days*24 + fcst_hours）
        horizon_days = max(route.days, 1)
        for wp in route.waypoints:
            day_offset = wp.day - 1
            d = (depart + timedelta(days=day_offset)).isoformat()
            resp = self._request(wp.lat, wp.lon, fcst_days=horizon_days)
            series = self._extract_series(resp)
            # 按目标日期过滤当天的逐时记录再聚合（响应是连续多日序列）
            day_series = [h for h in series if str(h.get("time", "")).startswith(d)]
            hourly = day_series or series
            if hourly:
                cells.append(self._aggregate_day(hourly, wp.id, d))
        return cells

    def fetch_hourly(self, wp: Waypoint, hours: int = 72) -> List[HourWeather]:
        """单点逐时预报（15min 粒度，最多 hours 小时）——实时监测用，不聚合。

        复用 _request 已传的 t_res=15min，直接消费原始序列。
        """
        resp = self._request(wp.lat, wp.lon, fcst_hours=hours)
        series = self._extract_series(resp)
        out: List[HourWeather] = []
        for h in series:
            try:
                out.append(HourWeather(
                    waypoint_id=wp.id,
                    datetime=str(h.get("time", "")),
                    t2m=round(float(h.get("t2m", 0)), 1),
                    ws10m=round(float(h.get("ws10m", 0)), 1),
                    wd10m=str(h.get("wd10m", "")),
                    rh2m=round(float(h.get("rh2m", 0)), 0),
                    tp_mm=round(float(h.get("tp", 0)), 1),
                    slp=round(float(h.get("slp", 0)), 1),
                ))
            except (TypeError, ValueError):
                continue
        return out


def get_source():
    return DemoSource() if WEATHER_DEMO_MODE else TianjiSource()

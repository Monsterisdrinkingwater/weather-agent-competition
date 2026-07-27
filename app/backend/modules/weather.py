"""天气数据源：
- TianjiSource：天机 API 逐点查询（拿到 Key 后自动启用）
- DemoSource：确定性模拟数据，随海拔/纬度/日期变化，支持情景注入

两者输出统一为 DayPointWeather 列表，上层不感知来源。
"""
import hashlib
import math
import time
from datetime import date, datetime, timedelta
from typing import List, Optional

import httpx

from config import TJ_API_KEY, TJ_API_BASE, TJ_SUBSCRIPTION_ID, WEATHER_DEMO_MODE
from models import Route, DayPointWeather, HourWeather, Waypoint

_WIND_DIRS = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]


# ── 天气主题分类（前端背景自适应用）────────────────────────
# 天机通用数据包没有天气现象码，用降水强度/湿度/气温推断天气大类

def _categorize(tp_mmhr: float, rh: float, t2m: float) -> str:
    """降水优先；无降水时按相对湿度分晴/多云/阴/雾。"""
    if tp_mmhr >= 0.1:
        if t2m <= 0.5:
            return "snow"
        return "heavyrain" if tp_mmhr >= 4 else "rain"
    if rh >= 95:
        return "fog"
    if rh >= 80:
        return "overcast"
    if rh >= 60:
        return "cloudy"
    return "clear"


def _is_night_now() -> bool:
    h = datetime.now().hour
    return h >= 19 or h < 6


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

    def theme_weather(self, lat: float, lon: float,
                      target: Optional[date] = None) -> dict:
        """单点天气主题（确定性模拟）：预报期内给预报，否则给“实时”。"""
        today = date.today()
        if target and today <= target <= today + timedelta(days=9):
            base = _season_base_temp(target, lat)
            roll = _hnoise(round(lat, 2), round(lon, 2), target, "rain", lo=0, hi=1)
            tp = max(0.0, (roll - 0.55)) * 6
            rh = 55 + roll * 40
            return {"mode": "forecast", "date": target.isoformat(),
                    "category": _categorize(tp, rh, base),
                    "temp_c": round(base, 1), "t_min": round(base - 5, 1),
                    "t_max": round(base + 5, 1), "is_night": False, "source": self.name}
        base = _season_base_temp(today, lat)
        roll = _hnoise(round(lat, 2), round(lon, 2), today, "rain", lo=0, hi=1)
        tp = max(0.0, (roll - 0.55)) * 6
        rh = 55 + roll * 40
        return {"mode": "realtime", "date": today.isoformat(),
                "category": _categorize(tp, rh, base),
                "temp_c": round(base, 1), "t_min": None, "t_max": None,
                "is_night": _is_night_now(), "source": self.name}


# ──────────────────────────── 天机真实数据源 ────────────────────────────

def _deg_to_dir(deg) -> str:
    """风向角度(°) → 八方位中文：实测 v2 接口 wd10m 返回的是角度数值。"""
    try:
        return _WIND_DIRS[int(((float(deg) + 22.5) % 360) // 45)]
    except (TypeError, ValueError):
        return ""


class TianjiSource:
    """天机新版 API（/v2）单点查询封装。
    鉴权：key + subscriptionId（订阅制，通用气象数据包 6 要素）。
    实测响应约定：wd10m 为角度(°)；psz 为地表气压(Pa)；tp 为降水强度(mm/hr)。
    """

    name = "tjweather"

    # 通用数据包 6 个地面要素（气压为 psz，非旧版 slp）
    _FIELDS = "t2m,ws10m,wd10m,psz,rh2m,tp"
    _MAX_FCST_DAYS = 10  # 通用数据包最长预报 10 天

    _MIN_GAP_S = 0.35    # 免费 key QPS 有限，逐点请求做最小间隔限速

    def __init__(self):
        self.client = httpx.Client(base_url=TJ_API_BASE.rstrip("/"), timeout=20)
        self._last_req = 0.0

    def _request(self, lat: float, lon: float, fcst_days: int = 0,
                 fcst_hours: int = 0, t_res: str = "1h") -> dict:
        """查询单点预报。
        日聚合用 1h（多日行程数据量小 4 倍），实时监测用 15min。
        429 限流时退避重试，避免多点位连环失败。
        """
        wait = self._MIN_GAP_S - (time.monotonic() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            self._last_req = time.monotonic()
            resp = self.client.get(
                "/v2",
                params={
                    "key": TJ_API_KEY,
                    "subscriptionId": TJ_SUBSCRIPTION_ID,
                    "loc": f"{lon},{lat}",           # 经度在前，纬度在后
                    "fields": self._FIELDS,
                    "fcst_days": str(fcst_days),
                    "fcst_hours": str(fcst_hours),
                    "t_res": t_res,
                    "tz": "8",                        # 北京时间
                },
            )
            if resp.status_code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 200:
            raise RuntimeError(f"天机 API 错误 {body.get('code')}: {body.get('message')}")
        return body

    @staticmethod
    def _aggregate_day(hourly: List[dict], waypoint_id: str, day: str,
                       interval_h: float = 1.0) -> DayPointWeather:
        """逐时 → 日聚合。hourly: [{t2m, ws10m, wd10m, psz, rh2m, tp}, ...]"""
        t = [h["t2m"] for h in hourly]
        ws = [h["ws10m"] for h in hourly]
        rh = [h["rh2m"] for h in hourly]
        # tp 是强度 mm/hr，×采样间隔时长才是日累计降水量
        tp = sum(h.get("tp", 0) for h in hourly) * interval_h
        # psz 单位 Pa → hPa 后再看趋势（阈值 1.5 hPa）
        p_first = hourly[0].get("psz", 0) / 100.0
        p_last = hourly[-1].get("psz", 0) / 100.0
        trend = "rising" if p_last - p_first > 1.5 else (
            "falling" if p_first - p_last > 1.5 else "steady")
        # 主导风向取风速最大时刻，角度转八方位
        wd = _deg_to_dir(max(hourly, key=lambda h: h["ws10m"]).get("wd10m"))
        return DayPointWeather(
            waypoint_id=waypoint_id, date=day,
            t_min=round(min(t), 1), t_max=round(max(t), 1),
            ws10m_max=round(max(ws), 1), wd10m=wd,
            rh2m_avg=round(sum(rh) / len(rh), 0), tp_mm=round(tp, 1),
            slp_trend=trend,
        )

    @staticmethod
    def _extract_series(resp: dict) -> List[dict]:
        """从天机 /v2 响应里取出逐时序列。

        响应结构：{code, message, data: {units, data: [{time, t2m, ...}], time_init}}
        """
        body = resp.get("data") or {}
        series = body.get("data") or []
        return series if isinstance(series, list) else []

    def fetch(self, route: Route, depart: date, scenario: str = "normal") -> List[DayPointWeather]:
        cells = []
        # 预报从起报时刻（今天）开始，预报长度必须覆盖到行程最后一天，
        # 而非只算行程天数（出发日在未来时两者差距很大）
        trip_end = depart + timedelta(days=max(route.days, 1) - 1)
        horizon = (trip_end - date.today()).days + 1
        horizon = min(self._MAX_FCST_DAYS, max(horizon, 1))
        for wp in route.waypoints:
            d = (depart + timedelta(days=wp.day - 1)).isoformat()
            resp = self._request(wp.lat, wp.lon, fcst_days=horizon, t_res="1h")
            series = self._extract_series(resp)
            # 只聚合目标日期当天的记录；超出预报期的日期直接缺格，
            # 绝不拿其它日期的数据充数
            day_series = [h for h in series if str(h.get("time", "")).startswith(d)]
            if day_series:
                cells.append(self._aggregate_day(day_series, wp.id, d, interval_h=1.0))
        return cells

    def fetch_hourly(self, wp: Waypoint, hours: int = 72) -> List[HourWeather]:
        """单点逐时预报（15min 粒度，最多 hours 小时）——实时监测用，不聚合。"""
        resp = self._request(wp.lat, wp.lon, fcst_hours=hours, t_res="15min")
        series = self._extract_series(resp)
        out: List[HourWeather] = []
        for h in series:
            try:
                out.append(HourWeather(
                    waypoint_id=wp.id,
                    datetime=str(h.get("time", "")),
                    t2m=round(float(h.get("t2m", 0)), 1),
                    ws10m=round(float(h.get("ws10m", 0)), 1),
                    wd10m=_deg_to_dir(h.get("wd10m")),
                    rh2m=round(float(h.get("rh2m", 0)), 0),
                    tp_mm=round(float(h.get("tp", 0)) * 0.25, 2),  # mm/hr × 0.25h → 该 15min 降水量
                    slp=round(float(h.get("psz", 0)) / 100.0, 1),  # Pa → hPa
                ))
            except (TypeError, ValueError):
                continue
        return out

    def theme_weather(self, lat: float, lon: float,
                      target: Optional[date] = None) -> dict:
        """单点天气主题：目标日期在 10 天预报期内用当日预报，
        否则（无日期/太久远/已过去）降级为当地实时天气，mode 字段标注来源。"""
        today = date.today()
        if target and today <= target <= today + timedelta(days=self._MAX_FCST_DAYS - 1):
            horizon = min(self._MAX_FCST_DAYS, (target - today).days + 1)
            resp = self._request(lat, lon, fcst_days=horizon, t_res="1h")
            day = [h for h in self._extract_series(resp)
                   if str(h.get("time", "")).startswith(target.isoformat())]
            if day:
                t = [float(h.get("t2m", 0)) for h in day]
                rh_avg = sum(float(h.get("rh2m", 50)) for h in day) / len(day)
                tp_max = max(float(h.get("tp", 0) or 0) for h in day)
                t_mid = sum(t) / len(t)
                return {"mode": "forecast", "date": target.isoformat(),
                        "category": _categorize(tp_max, rh_avg, t_mid),
                        "temp_c": round(t_mid, 1), "t_min": round(min(t), 1),
                        "t_max": round(max(t), 1), "is_night": False, "source": self.name}
        # 实时：取预报序列首个 15min 记录 ≈ 当前天气
        resp = self._request(lat, lon, fcst_hours=1, t_res="15min")
        series = self._extract_series(resp)
        if not series:
            raise RuntimeError("天机 API 未返回数据")
        h0 = series[0]
        t_now = float(h0.get("t2m", 15))
        return {"mode": "realtime", "date": today.isoformat(),
                "category": _categorize(float(h0.get("tp", 0) or 0),
                                         float(h0.get("rh2m", 50)), t_now),
                "temp_c": round(t_now, 1), "t_min": None, "t_max": None,
                "is_night": _is_night_now(), "source": self.name}


def get_source():
    return DemoSource() if WEATHER_DEMO_MODE else TianjiSource()

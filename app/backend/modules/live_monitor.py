"""活动期实时监测引擎：徒步/越野跑进行中的天气预警。

两类告警（均基于 15min 粒度小时级预报）：
- 前瞻预警：未来 LOOKAHEAD_H 小时内将出现的危险天气
  （短时强降水 / 大风 / 3h 骤冷 / 冰点 / 气压骤降=天气系统逼近）
- 预报修正：同一时刻的预报值被气象台显著下修（温度↓/风力↑）

去重：每个 (点位, 告警类型, 小时槽) 只推送一次，state 在 SSE 连接生命周期内保持。
"""
import logging
from typing import Dict, List

from models import HourWeather, Route, Waypoint

logger = logging.getLogger(__name__)

LOOKAHEAD_H = 6          # 前瞻窗口（小时）
_QUARTERS_3H = 12        # 3 小时 = 12 个 15min 槽

# 阈值
RAIN_WARN_MMH = 3.0      # 降水强度 mm/h：明显降水
RAIN_DANGER_MMH = 8.0    # 短时强降水
WIND_WARN_MS = 10.8      # 6 级风
WIND_DANGER_MS = 13.9    # 7 级风
TEMP_DROP_3H = 5.0       # 3h 内降温幅度
SLP_DROP_3H = 2.5        # 3h 气压降幅 hPa（系统逼近）
REVISE_TEMP = 3.0        # 同时刻预报温度下修
REVISE_WIND = 4.0        # 同时刻预报风速上修


def _alert(severity: str, kind: str, wp: Waypoint, dt: str,
           title: str, detail: str, suggestion: str) -> dict:
    return {"severity": severity, "kind": kind,
            "waypoint_name": wp.name, "datetime": dt,
            "title": title, "detail": detail, "suggestion": suggestion}


def _push_once(sent: set, alerts: List[dict], a: dict, wp: Waypoint) -> None:
    """按 (点位|类型|小时槽) 去重后入列。"""
    key = "{}|{}|{}".format(wp.id, a["kind"], a["datetime"][:13])
    if key not in sent:
        sent.add(key)
        alerts.append(a)


def _forward_alerts(wp: Waypoint, cells: List[HourWeather], sent: set) -> List[dict]:
    """前瞻预警：扫描未来窗口内的危险天气。"""
    alerts: List[dict] = []
    drop_reported = False   # 骤冷随窗口滑动会重复命中，一轮只报一次
    for i, h in enumerate(cells):
        # 1) 短时强降水（tp_mm 为 15min 降水量 → ×4 得 mm/h 强度）
        rate = h.tp_mm * 4
        if rate >= RAIN_WARN_MMH:
            sev = "danger" if rate >= RAIN_DANGER_MMH else "warning"
            _push_once(sent, alerts, _alert(
                sev, "rain_ahead", wp, h.datetime,
                "{} 即将出现{}降水".format(wp.name, "强" if sev == "danger" else ""),
                "{} 预计降水强度 {:.1f}mm/h".format(h.datetime[11:16], rate),
                "提前穿好防水层收好电子设备；沟谷/溪流路段警惕水位上涨"), wp)

        # 2) 大风
        if h.ws10m >= WIND_WARN_MS:
            sev = "danger" if h.ws10m >= WIND_DANGER_MS else "warning"
            _push_once(sent, alerts, _alert(
                sev, "wind_ahead", wp, h.datetime,
                "{} 大风预警".format(wp.name),
                "{} 风速预计达 {:.1f}m/s（{}）".format(
                    h.datetime[11:16], h.ws10m, h.wd10m or "-"),
                "避开垭口/山脊暴露段，行进注意重心，停留选背风处"), wp)

        # 3) 冰点
        if h.t2m <= 0:
            _push_once(sent, alerts, _alert(
                "warning", "freeze_ahead", wp, h.datetime,
                "{} 气温将跌破冰点".format(wp.name),
                "{} 气温预计 {:.1f}°C".format(h.datetime[11:16], h.t2m),
                "补充保暖层，注意路面结冰；保持进食维持产热"), wp)

        # 4) 3h 骤冷：当前时刻 vs 未来 3h 最低
        window = cells[i:i + _QUARTERS_3H]
        if not drop_reported and len(window) >= 4:
            coldest = min(window, key=lambda x: x.t2m)
            drop = h.t2m - coldest.t2m
            if drop >= TEMP_DROP_3H:
                sev = "danger" if drop >= 8 or coldest.t2m <= 0 else "warning"
                _push_once(sent, alerts, _alert(
                    sev, "drop_ahead", wp, coldest.datetime,
                    "{} 未来 3 小时气温骤降".format(wp.name),
                    "{} {:.1f}°C → {} {:.1f}°C（降 {:.1f}°C）".format(
                        h.datetime[11:16], h.t2m,
                        coldest.datetime[11:16], coldest.t2m, drop),
                    "立即穿上保暖/防风层，别等冷了再穿；失温风险随风雨叠加"), wp)
                drop_reported = True

    # 5) 气压骤降（天气系统逼近的先兆）
    if len(cells) > _QUARTERS_3H:
        p_now, p_3h = cells[0].slp, cells[_QUARTERS_3H].slp
        if p_now - p_3h >= SLP_DROP_3H:
            _push_once(sent, alerts, _alert(
                "warning", "slp_drop", wp, cells[_QUARTERS_3H].datetime,
                "{} 气压快速下降".format(wp.name),
                "未来 3h 气压 {:.1f} → {:.1f}hPa，天气系统逼近".format(p_now, p_3h),
                "警惕风雨突至，评估是否提前到达营地/下撤点"), wp)
    return alerts


def _revision_alerts(wp: Waypoint, cells: List[HourWeather],
                     prev: Dict[str, HourWeather], sent: set) -> List[dict]:
    """预报修正检测：同一时刻的预报值与上一轮相比被显著改差。"""
    alerts: List[dict] = []
    for h in cells:
        key = wp.id + "|" + h.datetime
        old = prev.get(key)
        if old is not None:
            if old.t2m - h.t2m >= REVISE_TEMP:
                _push_once(sent, alerts, _alert(
                    "warning", "revise_temp", wp, h.datetime,
                    "{} 气温预报大幅下修".format(wp.name),
                    "{} 预报从 {:.1f}°C 修正为 {:.1f}°C".format(
                        h.datetime[11:16], old.t2m, h.t2m),
                    "实况可能比出发时预期更冷，检查保暖余量"), wp)
            if h.ws10m - old.ws10m >= REVISE_WIND:
                _push_once(sent, alerts, _alert(
                    "warning", "revise_wind", wp, h.datetime,
                    "{} 风力预报上修".format(wp.name),
                    "{} 预报从 {:.1f} 修正为 {:.1f}m/s".format(
                        h.datetime[11:16], old.ws10m, h.ws10m),
                    "重新评估暴露路段通过时机"), wp)
        prev[key] = h
    return alerts


def scan(route: Route, source, state: dict) -> List[dict]:
    """跑一轮全线监测。state 由调用方在 SSE 连接期间持有：
    {"sent": set(去重键), "prev": {wp|time: HourWeather}}
    """
    sent: set = state.setdefault("sent", set())
    prev: Dict[str, HourWeather] = state.setdefault("prev", {})

    alerts: List[dict] = []
    for wp in route.waypoints:
        try:
            cells = source.fetch_hourly(wp, LOOKAHEAD_H)
        except Exception as e:   # 单点限流/超时不应毁掉整轮扫描
            logger.warning("实时监测拉取 %s 失败: %s", wp.name, e)
            continue
        if not cells:
            continue
        alerts += _forward_alerts(wp, cells, sent)
        alerts += _revision_alerts(wp, cells, prev, sent)

    # 防内存无限增长：sent/prev 超限时只保留最新一半
    if len(sent) > 2000:
        state["sent"] = set(list(sent)[-1000:])
    if len(prev) > 4000:
        state["prev"] = dict(list(prev.items())[-2000:])

    order = {"danger": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: (order.get(a["severity"], 9), a["datetime"]))
    return alerts

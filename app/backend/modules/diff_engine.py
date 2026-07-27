"""快照 diff 引擎 + 装备交叉检查 → AlertEvent 列表。

两类事件：
- 变化类（需要两份快照）：温度骤降/降水新增或激增/风力跃升
- 缺口类（单快照即可）：装备参数 vs 最新预报的硬性缺口、绝对危险阈值
"""
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config import THRESHOLDS as TH
from models import (AlertEvent, DayPointWeather, GearItem, Plan, Route,
                    WeatherSnapshot)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_event(plan_id: str, kind: str, severity: str, wp_id: str, wp_name: str,
               date: str, title: str, detail: str,
               gear_affected: Optional[List[str]] = None,
               suggestion: str = "") -> AlertEvent:
    return AlertEvent(
        id=uuid.uuid4().hex[:10], plan_id=plan_id, created_at=_now(),
        kind=kind, severity=severity, waypoint_id=wp_id, waypoint_name=wp_name,
        date=date, title=title, detail=detail,
        gear_affected=gear_affected or [], suggestion=suggestion,
    )


def _cell_map(snap: WeatherSnapshot) -> Dict[Tuple[str, str], DayPointWeather]:
    return {(c.waypoint_id, c.date): c for c in snap.cells}


def _wp_names(route: Route) -> Dict[str, str]:
    return {w.id: w.name for w in route.waypoints}


# ── 变化检测：old vs new 快照 ────────────────────────────────────

def diff_snapshots(plan: Plan, route: Route,
                   old: WeatherSnapshot, new: WeatherSnapshot) -> List[AlertEvent]:
    events: List[AlertEvent] = []
    names = _wp_names(route)
    old_map = _cell_map(old)

    for cell in new.cells:
        key = (cell.waypoint_id, cell.date)
        prev = old_map.get(key)
        if not prev:
            continue
        wp_name = names.get(cell.waypoint_id, cell.waypoint_id)

        # 最低温骤降
        drop = prev.t_min - cell.t_min
        if drop >= TH["t_min_drop"]:
            sev = "danger" if drop >= 6 or cell.t_min <= 0 else "warning"
            events.append(_new_event(
                plan.id, "temp_drop", sev, cell.waypoint_id, wp_name, cell.date,
                "{} 夜间最低温大幅下调".format(wp_name),
                "最低温从 {:.1f}°C 下调至 {:.1f}°C（降幅 {:.1f}°C）".format(
                    prev.t_min, cell.t_min, drop),
                suggestion="核对睡袋温标与保暖层，必要时升级睡袋或增加羽绒/保暖衣物",
            ))

        # 降水：无 → 有，或激增
        if prev.tp_mm < 1 and cell.tp_mm >= TH["precip_new_mm"]:
            sev = "danger" if cell.tp_mm >= 25 else "warning"
            events.append(_new_event(
                plan.id, "precip_new", sev, cell.waypoint_id, wp_name, cell.date,
                "{} 新增降水过程".format(wp_name),
                "原预报无有效降水，现预报日降水 {:.1f}mm".format(cell.tp_mm),
                suggestion="检查雨具与背包防水；土质/岩石路段湿滑，重新评估当日行程节奏",
            ))
        elif cell.tp_mm - prev.tp_mm >= TH["precip_jump_mm"]:
            sev = "danger" if cell.tp_mm >= 25 else "warning"
            events.append(_new_event(
                plan.id, "precip_jump", sev, cell.waypoint_id, wp_name, cell.date,
                "{} 降水量显著上调".format(wp_name),
                "日降水从 {:.1f}mm 上调至 {:.1f}mm".format(prev.tp_mm, cell.tp_mm),
                suggestion="警惕溪流上涨与横渡风险，预留撤退路线",
            ))

        # 风力跃升
        jump = cell.ws10m_max - prev.ws10m_max
        if jump >= TH["wind_jump_ms"]:
            sev = "danger" if cell.ws10m_max >= TH["wind_danger_ms"] else "warning"
            events.append(_new_event(
                plan.id, "wind_jump", sev, cell.waypoint_id, wp_name, cell.date,
                "{} 风力明显增强".format(wp_name),
                "最大风速从 {:.1f}m/s 升至 {:.1f}m/s（{}风）".format(
                    prev.ws10m_max, cell.ws10m_max, cell.wd10m),
                suggestion="垭口/山脊段避开大风时段通过；检查帐篷抗风与地钉配置",
            ))
    return events


# ── 装备交叉检查：最新快照 vs 装备参数 ──────────────────────────

def _gear_by_cat(plan: Plan, cat: str) -> List[GearItem]:
    return [g for g in plan.gear if g.category == cat]


def gear_gap_check(plan: Plan, route: Route, snap: WeatherSnapshot) -> List[AlertEvent]:
    events: List[AlertEvent] = []
    names = _wp_names(route)
    kinds = {w.id: w.kind for w in route.waypoints}

    # 全程最低温出现的点（营地类点位优先——夜间才用睡袋）
    camp_cells = [c for c in snap.cells if kinds.get(c.waypoint_id) in ("camp", "start")]
    check_cells = camp_cells or snap.cells

    # 1) 睡袋温标 vs 营地最低温（仅徒步：越野跑不过夜）
    if plan.activity == "hiking":
        bags = _gear_by_cat(plan, "sleep")
        coldest = min(check_cells, key=lambda c: c.t_min, default=None)
        if coldest:
            wp_name = names.get(coldest.waypoint_id, "")
            if not bags:
                events.append(_new_event(
                    plan.id, "gear_gap", "danger", coldest.waypoint_id, wp_name,
                    coldest.date, "装备清单缺少睡袋",
                    "全程最低温 {:.1f}°C（{}），未发现睡袋".format(coldest.t_min, wp_name),
                    suggestion="多日线路必须携带睡袋",
                ))
            else:
                for bag in bags:
                    comfort = bag.params.get("comfort_c")
                    if comfort is None:
                        continue
                    margin = coldest.t_min - float(comfort)
                    if margin < 0:
                        events.append(_new_event(
                            plan.id, "gear_gap", "danger", coldest.waypoint_id,
                            wp_name, coldest.date,
                            "睡袋温标不足：{}".format(bag.name),
                            "{} 舒适温标 {}°C，营地最低温预报 {:.1f}°C，缺口 {:.1f}°C".format(
                                bag.name, comfort, coldest.t_min, -margin),
                            gear_affected=[bag.name],
                            suggestion="升级更低温标睡袋，或加羽绒服+抓绒睡+热水壶补差",
                        ))
                    elif margin < 3:
                        events.append(_new_event(
                            plan.id, "gear_gap", "warning", coldest.waypoint_id,
                            wp_name, coldest.date,
                            "睡袋温标余量偏小：{}".format(bag.name),
                            "舒适温标 {}°C vs 最低温 {:.1f}°C，余量仅 {:.1f}°C".format(
                                comfort, coldest.t_min, margin),
                            gear_affected=[bag.name],
                            suggestion="女性/怕冷体质建议按 +5°C 余量准备，可加睡袋内胆",
                        ))

    # 2) 降水 vs 雨具
    wet = [c for c in snap.cells if c.tp_mm >= TH["precip_new_mm"]]
    if wet:
        rains = _gear_by_cat(plan, "rain")
        worst = max(wet, key=lambda c: c.tp_mm)
        wp_name = names.get(worst.waypoint_id, "")
        real_rain = [g for g in rains if float(g.params.get("waterproof_mm", 0) or 0) >= 5000]
        if not real_rain:
            events.append(_new_event(
                plan.id, "gear_gap", "danger", worst.waypoint_id, wp_name, worst.date,
                "有降水过程但缺少有效防水层",
                "{} {} 预报日降水 {:.1f}mm，清单中无防水指数 ≥5000mm 的雨具".format(
                    worst.date, wp_name, worst.tp_mm),
                gear_affected=[g.name for g in rains],
                suggestion="携带硬壳或雨披；降雨+大风组合是失温头号诱因",
            ))

    # 3) 大风 vs 帐篷抗风（徒步）
    if plan.activity == "hiking":
        windy_camps = [c for c in camp_cells if c.ws10m_max >= 10]
        if windy_camps:
            tents = _gear_by_cat(plan, "shelter")
            worst = max(windy_camps, key=lambda c: c.ws10m_max)
            wp_name = names.get(worst.waypoint_id, "")
            for tent in tents:
                rating = float(tent.params.get("wind_ms", 0) or 0)
                if rating and worst.ws10m_max > rating:
                    events.append(_new_event(
                        plan.id, "gear_gap", "warning", worst.waypoint_id, wp_name,
                        worst.date, "营地风速接近帐篷抗风上限",
                        "{} 最大风速 {:.1f}m/s，{} 标称抗风约 {:.0f}m/s".format(
                            wp_name, worst.ws10m_max, tent.name, rating),
                        gear_affected=[tent.name],
                        suggestion="选背风营地，全地钉+防风绳，必要时并帐扎营",
                    ))

    # 4) 绝对危险阈值（无论装备）
    for c in snap.cells:
        wp_name = names.get(c.waypoint_id, "")
        if c.t_min <= TH["t_min_danger"]:
            events.append(_new_event(
                plan.id, "danger", "danger", c.waypoint_id, wp_name, c.date,
                "{} 出现极端低温".format(wp_name),
                "最低温预报 {:.1f}°C".format(c.t_min),
                suggestion="重新评估出行窗口，考虑改期",
            ))
        if c.ws10m_max >= TH["wind_danger_ms"] and kinds.get(c.waypoint_id) in ("pass", "peak"):
            events.append(_new_event(
                plan.id, "danger", "danger", c.waypoint_id, wp_name, c.date,
                "{} 大风达 7 级以上".format(wp_name),
                "最大风速 {:.1f}m/s，垭口/山脊暴露感强".format(c.ws10m_max),
                suggestion="调整行程避开该时段翻越，或从备选路线绕行",
            ))
    return events


# ── 规划期风险分析：单快照即可，不依赖历史对比 ──────────────

def _trim_by_kind(events: List[AlertEvent], kind: str, keep: int) -> List[AlertEvent]:
    """同类风险只保留最严重的 keep 条，避免长线路多点位刷屏。"""
    same = [e for e in events if e.kind == kind]
    if len(same) <= keep:
        return events
    order = {"danger": 0, "warning": 1, "info": 2}
    same.sort(key=lambda e: (order.get(e.severity, 9), e.date))
    drop = set(id(e) for e in same[keep:])
    return [e for e in events if id(e) not in drop]


def forecast_risk_check(plan: Plan, route: Route, snap: WeatherSnapshot) -> List[AlertEvent]:
    """规划阶段风险扫描：昼夜温差 / 行程内骤变 / 高温 / 冰点。"""
    events: List[AlertEvent] = []
    names = _wp_names(route)

    heat_th = TH["t_heat"] - (2.0 if plan.activity == "trailrun" else 0.0)
    for c in snap.cells:
        wp_name = names.get(c.waypoint_id, c.waypoint_id)

        # 1) 昼夜温差大：午后与凌晨体感差异巨大
        rng = c.t_max - c.t_min
        if rng >= TH["t_range_big"]:
            events.append(_new_event(
                plan.id, "temp_range", "info", c.waypoint_id, wp_name, c.date,
                "{} 昼夜温差达 {:.0f}°C".format(wp_name, rng),
                "当日 {:.1f}~{:.1f}°C，午后与夜间/凌晨体感差异巨大".format(c.t_min, c.t_max),
                suggestion="三层穿衣法：速干打底+保暖中层+防风外层，随行程增减",
            ))

        # 2) 冰点风险（极端低温已由 danger 阈值单独报，这里不重复）
        if TH["t_min_danger"] < c.t_min <= 0:
            events.append(_new_event(
                plan.id, "freeze", "warning", c.waypoint_id, wp_name, c.date,
                "{} 夜间温度跌破冰点".format(wp_name),
                "最低温预报 {:.1f}°C，清晨路面/木栈道可能结冰或结霜".format(c.t_min),
                suggestion="水袋管防冻，备厚手套；结冰路段考虑携带冰爪",
            ))

        # 3) 高温风险（越野跑对热应激更敏感）
        if c.t_max >= heat_th:
            events.append(_new_event(
                plan.id, "heat", "warning", c.waypoint_id, wp_name, c.date,
                "{} 高温风险".format(wp_name),
                "最高温预报 {:.1f}°C，暴晒路段中暑/热射病风险上升".format(c.t_max),
                suggestion="避开 11-15 点暴晒路段，加大补水并补充电解质",
            ))

    # 4) 行程内骤变：相邻两天全线最低温骤降（冷空气过境）
    by_date: Dict[str, List[DayPointWeather]] = {}
    for c in snap.cells:
        by_date.setdefault(c.date, []).append(c)
    dates = sorted(by_date)
    for d1, d2 in zip(dates, dates[1:]):
        lo1 = min(c.t_min for c in by_date[d1])
        cold = min(by_date[d2], key=lambda c: c.t_min)
        drop = lo1 - cold.t_min
        if drop >= TH["trip_drop"]:
            sev = "danger" if drop >= 8 or cold.t_min <= 0 else "warning"
            events.append(_new_event(
                plan.id, "trip_swing", sev, cold.waypoint_id,
                names.get(cold.waypoint_id, cold.waypoint_id), d2,
                "行程中气温骤降：{} → {}".format(d1[5:], d2[5:]),
                "全线最低温从 {:.1f}°C 降至 {:.1f}°C（降幅 {:.1f}°C），疑似冷空气过境".format(
                    lo1, cold.t_min, drop),
                suggestion="按行程后半段的低温准备保暖，必要时压缩后半段行程提前下撤",
            ))

    # 同类信息只留最重要的几条，避免多点位长线路刷屏
    for kind, keep in (("temp_range", 2), ("freeze", 2), ("heat", 2)):
        events = _trim_by_kind(events, kind, keep)
    return events


def run_reconcile(plan: Plan, route: Route, old: Optional[WeatherSnapshot],
                  new: WeatherSnapshot) -> List[AlertEvent]:
    """完整对账：变化检测（有历史快照时）+ 装备缺口检查 + 规划期风险分析。按严重度排序。"""
    events: List[AlertEvent] = []
    if old is not None:
        events += diff_snapshots(plan, route, old, new)
    events += gear_gap_check(plan, route, new)
    events += forecast_risk_check(plan, route, new)
    order = {"danger": 0, "warning": 1, "info": 2}
    events.sort(key=lambda e: (order.get(e.severity, 9), e.date))
    return events

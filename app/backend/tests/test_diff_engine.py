"""diff_engine 核心判定逻辑的最小聚焦测试。

覆盖：
- diff_snapshots 三条触发路径：温度骤降 / 降水新增 / 风力跃升 + 一条不触发负例
- gear_gap_check 睡袋温标缺口路径 + 一条余量充足的负例

全部使用手工构造的 WeatherSnapshot，数值确定，不依赖外部服务。
"""
from models import (DayPointWeather, GearItem, Plan, Route, Waypoint,
                    WeatherSnapshot)
from modules.diff_engine import diff_snapshots, gear_gap_check


# ── 构造辅助 ─────────────────────────────────────────────────────

def _route() -> Route:
    return Route(
        id="r_test", name="测试线路", activity="hiking", region="测试",
        days=1, distance_km=10.0, ascent_m=500, difficulty="入门",
        summary="", waypoints=[
            Waypoint(id="wp1", name="测试营地", lat=27.0, lon=100.0,
                     elevation=3000, kind="camp", day=1),
        ],
    )


def _plan(gear=None) -> Plan:
    return Plan(id="p_test", route_id="r_test", activity="hiking",
                depart_date="2026-08-01", gear=gear or [])


def _cell(t_min: float = 5.0, tp_mm: float = 0.0,
          ws10m_max: float = 3.0) -> DayPointWeather:
    return DayPointWeather(
        waypoint_id="wp1", date="2026-08-01",
        t_min=t_min, t_max=t_min + 10,
        ws10m_max=ws10m_max, wd10m="西北",
        rh2m_avg=60, tp_mm=tp_mm,
    )


def _snap(snap_id: str, cell: DayPointWeather) -> WeatherSnapshot:
    return WeatherSnapshot(id=snap_id, plan_id="p_test",
                           taken_at="2026-07-25T08:00:00",
                           source="demo", cells=[cell])


def _diff(old_cell: DayPointWeather, new_cell: DayPointWeather):
    return diff_snapshots(_plan(), _route(),
                          _snap("s_old", old_cell), _snap("s_new", new_cell))


# ── diff_snapshots：三条触发路径 ─────────────────────────────────

def test_temp_drop_triggers():
    """最低温从 5.0 降到 1.0（降幅 4 ≥ 阈值 3）→ temp_drop 事件。"""
    events = _diff(_cell(t_min=5.0), _cell(t_min=1.0))
    assert [e.kind for e in events] == ["temp_drop"]
    assert events[0].severity == "warning"  # 降幅 <6 且 t_min >0
    assert events[0].waypoint_id == "wp1"


def test_precip_new_triggers():
    """降水从 0mm 到 5mm（≥ 阈值 3）→ precip_new 事件。"""
    events = _diff(_cell(tp_mm=0.0), _cell(tp_mm=5.0))
    assert [e.kind for e in events] == ["precip_new"]
    assert events[0].severity == "warning"  # <25mm


def test_wind_jump_triggers():
    """风速从 3.0 升到 8.0（增幅 5 ≥ 阈值 4）→ wind_jump 事件。"""
    events = _diff(_cell(ws10m_max=3.0), _cell(ws10m_max=8.0))
    assert [e.kind for e in events] == ["wind_jump"]
    assert events[0].severity == "warning"  # 未达 13.9m/s 危险线


def test_diff_below_thresholds_no_event():
    """负例：降温 2°C、降水 +2mm、风速 +3m/s 均低于阈值 → 不触发。"""
    events = _diff(_cell(t_min=5.0, tp_mm=0.0, ws10m_max=3.0),
                   _cell(t_min=3.0, tp_mm=2.0, ws10m_max=6.0))
    assert events == []


# ── gear_gap_check：睡袋温标缺口路径 ─────────────────────────────

def _bag(comfort_c: float) -> GearItem:
    return GearItem(name="测试睡袋", category="sleep",
                    params={"comfort_c": comfort_c})


def test_sleeping_bag_gap_triggers():
    """睡袋舒适温标 0°C，营地最低温 -5°C → 缺口 5°C，danger 级 gear_gap。"""
    plan = _plan(gear=[_bag(comfort_c=0)])
    events = gear_gap_check(plan, _route(), _snap("s1", _cell(t_min=-5.0)))
    gaps = [e for e in events if e.kind == "gear_gap"]
    assert len(gaps) == 1
    assert gaps[0].severity == "danger"
    assert "测试睡袋" in gaps[0].gear_affected


def test_sleeping_bag_enough_margin_no_event():
    """负例：睡袋温标 -10°C，最低温 -5°C，余量 5°C ≥ 3 → 不触发任何事件。"""
    plan = _plan(gear=[_bag(comfort_c=-10)])
    events = gear_gap_check(plan, _route(), _snap("s1", _cell(t_min=-5.0)))
    assert events == []

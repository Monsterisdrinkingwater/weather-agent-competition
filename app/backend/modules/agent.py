"""Agent 层：把快照 + 提醒事件转成人话对账报告。
优先 LLM 生成，无 Key / 失败时用结构化模板兜底，保证 demo 永不空白。
"""
from typing import List, Optional

from models import AlertEvent, Plan, Route, WeatherSnapshot
from modules.gear import _llm_chat

_REPORT_SYSTEM = """你是资深户外领队兼气象顾问，为用户的{activity}计划做出行对账简报。
要求：
1. 先一句话总评（可以走/需调整/建议改期），语气专业但不吓唬人
2. 逐条解读高严重度提醒，落到具体装备和行动（带上装备名和数值）
3. 严格区分活动类型：多日徒步关注睡袋温标/帐篷抗风/营地夜温；
   越野跑不过夜、不需要睡袋帐篷，重点是强制装备（救生毯/头灯/防水外套/保暖层）、
   补水补盐与关门时间内的天气窗口
4. 提到任何装备都要给出可量化的性能维度，越细越好：保暖（温标/蓬松度/充绒量）、
   防水（静水压mm）、透气（g/m²/24h）、抗风、齿深、克重等，不要只报装备名
5. 最后给一段"出发前 48 小时再确认清单"
6. 用 Markdown，总长 350 字以内，中文。"""


def _template_report(plan: Plan, route: Route, snap: WeatherSnapshot,
                     events: List[AlertEvent]) -> str:
    dangers = [e for e in events if e.severity == "danger"]
    warnings = [e for e in events if e.severity == "warning"]
    if dangers:
        verdict = "⛔ **建议调整计划**——存在 {} 项高危提醒".format(len(dangers))
    elif warnings:
        verdict = "⚠️ **可以出行，但需针对性调整**——{} 项注意事项".format(len(warnings))
    else:
        verdict = "✅ **天气窗口良好**——按计划出发，保持常规安全冗余"

    lines = ["### 对账结论", verdict, ""]
    if events:
        lines.append("### 重点提醒")
        for e in (dangers + warnings)[:5]:
            lines.append("- **{}**（{} · {}）：{}".format(e.title, e.date, e.waypoint_name, e.detail))
            if e.suggestion:
                lines.append("  - 建议：{}".format(e.suggestion))
    t_mins = [c.t_min for c in snap.cells]
    tps = [c.tp_mm for c in snap.cells]
    lines += [
        "", "### 出发前 48 小时再确认",
        "- 全程最低温 {:.1f}°C / 最大日降水 {:.1f}mm，重查一次最新预报".format(
            min(t_mins), max(tps)) if t_mins else "- 重查一次最新预报",
        ("- 逐件核对睡袋温标、防水层、保暖层与手套帽子" if plan.activity == "hiking"
         else "- 逐件核对强制装备：救生毯、头灯、防水外套、电解质与携水"),
        "- 向同伴同步撤退点与失联预案",
    ]
    return "\n".join(lines)


def build_report(plan: Plan, route: Route, snap: WeatherSnapshot,
                 events: List[AlertEvent], prev: Optional[WeatherSnapshot]) -> dict:
    """返回 {report_md, generated_by}"""
    activity = "多日徒步" if plan.activity == "hiking" else "越野跑"
    ev_lines = "\n".join(
        "- [{}] {}｜{}｜{}｜{}｜建议：{}".format(
            e.severity, e.date, e.waypoint_name, e.title, e.detail, e.suggestion)
        for e in events[:8]) or "- 无提醒事件"
    gear_lines = "\n".join(
        "- {}（{}，参数 {}）".format(g.name, g.category, g.params) for g in plan.gear) or "- 未录入"
    wx_lines = "\n".join(
        "- {} {}: {:.1f}~{:.1f}°C, 风 {:.1f}m/s, 降水 {:.1f}mm".format(
            c.date, c.waypoint_id, c.t_min, c.t_max, c.ws10m_max, c.tp_mm)
        for c in snap.cells[:14])
    user_prompt = (
        "线路：{}（{}，{} 天，{}km，累计爬升 {}m）\n出发日期：{}\n\n"
        "装备清单：\n{}\n\n提醒事件：\n{}\n\n沿线天气（节选）：\n{}"
    ).format(route.name, route.region, route.days, route.distance_km,
             route.ascent_m, plan.depart_date, gear_lines, ev_lines, wx_lines)

    llm_out = _llm_chat(_REPORT_SYSTEM.format(activity=activity), user_prompt)
    if llm_out:
        return {"report_md": llm_out, "generated_by": "llm"}
    return {"report_md": _template_report(plan, route, snap, events),
            "generated_by": "template"}

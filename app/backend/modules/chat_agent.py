"""对话编排：ReAct 循环 + SSE 事件流。

核心循环（run_chat）：
  用户消息 → LLM(带 tools, 非流式决策)
    ├─ 返回 tool_calls → 执行工具 → 结果回灌消息历史 → 再决策（最多 N 轮）
    └─ 返回 content     → LLM(纯文本, 流式) 把答案逐 token yield 给前端

设计要点：
- 工具决策用非流式（魔搭 OpenAI 兼容接口 stream+tools 易出错）；
- 最终回复用流式（打字机效果）；
- 通过 SSE 事件类型告知前端当前阶段：
    tool_start / tool_end / token / done / error
- 对话历史从 storage 读取并回写，跨请求保持上下文。
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

from models import Conversation, Message
from modules.agent_tools import TOOLS, ToolRuntime, run_tool, tool_label
from modules.gear import llm_chat_stream, llm_chat_with_tools, require_llm

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 5    # 单次对话最多工具调用轮数，防失控


_CHAT_SYSTEM = """你是「行山对账」的户外出行规划助手，专长是帮用户规划徒步和越野跑出行。

你的工作方式（按需调用工具，不要一次性问完所有信息）：
1. 先了解用户想去哪、什么活动（多日徒步还是越野跑）、什么时间。
2. 用 search_routes 推荐合适的线路，结合用户体能给建议。
3. 聊装备：问用户有什么或打算带什么，必要时用 parse_gear_list 帮忙识别参数。
4. 信息齐全（线路+日期+装备）且用户确认后，用 create_plan 创建计划——这会自动生成首份天气对账。
5. 用户想看天气就调 check_weather_now。

沟通风格：
- 像懂户外的朋友聊天，专业但不啰嗦。一次只问 1-2 个问题，别列清单。
- 推荐线路时说清楚为什么适合（难度/季节/景观），给 2-3 个选择。
- 装备聊到关键缺失（如多日线无睡袋）要点出来，但别像查账。
- 天气有风险时要明确提示严重度，给出具体建议（装备/改期/换路线）。
- 不要编造未通过工具获取的数据。线路和天气信息必须来自工具结果。

当用户用一句话就能回答时，直接回答，不必每次都调工具。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sse(event_type: str, data: Dict[str, Any]) -> str:
    """构造一条 SSE 事件。前端按 event 字段分发渲染。"""
    return "event: {}\ndata: {}\n\n".format(event_type, json.dumps(data, ensure_ascii=False))


# ── 消息历史存取 ──────────────────────────────────────────────────

def _load_history(storage, conv: Conversation) -> List[Dict[str, Any]]:
    """从 storage 读出对话的 OpenAI 格式消息历史。"""
    history: List[Dict[str, Any]] = []
    for mid in conv.messages:
        m_data = storage.get("messages", mid)
        if not m_data:
            continue
        m = Message(**m_data)
        msg: Dict[str, Any] = {"role": m.role}
        if m.content:
            msg["content"] = m.content
        if m.tool_calls:
            msg["tool_calls"] = m.tool_calls
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        history.append(msg)
    return history


def _save_message(storage, conv: Conversation, role: str, content: str = "",
                  tool_calls: Optional[List[Dict]] = None,
                  tool_call_id: str = "", tool_name: str = "") -> Message:
    """落库一条消息并挂到对话上。"""
    msg = Message(
        id=uuid.uuid4().hex[:10], conversation_id=conv.id, role=role,
        content=content, tool_calls=tool_calls or [],
        tool_call_id=tool_call_id, tool_name=tool_name, created_at=_now(),
    )
    storage.put("messages", msg.id, msg.model_dump())
    conv.messages.append(msg.id)
    conv.updated_at = _now()
    storage.put("conversations", conv.id, conv.model_dump())
    return msg


def _extract_args(tc: Dict[str, Any]) -> Dict[str, Any]:
    """tool_calls 里的 arguments 是 JSON 字符串，解析成 dict。"""
    fn = tc.get("function", {})
    raw = fn.get("arguments", "{}")
    try:
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError:
        return {}


def _tool_call_id(tc: Dict[str, Any]) -> str:
    return tc.get("id", "")


def _tool_name(tc: Dict[str, Any]) -> str:
    return tc.get("function", {}).get("name", "")


# ── 主循环：SSE 事件生成器 ────────────────────────────────────────

def run_chat(storage, conv: Conversation, user_text: str,
             rt: ToolRuntime) -> Iterator[str]:
    """处理一条用户消息，yield SSE 事件流。

    前端收到的 event 类型：
      tool_start {tool_name, label, args}   —— 工具开始（显示"正在…")
      tool_end   {tool_name, result_brief}  —— 工具结束
      token      {text}                     —— 流式回复片段
      done       {conversation_id, plan_id?}—— 结束
      error      {message}                  —— 出错
    """
    try:
        require_llm()
    except RuntimeError as e:
        yield _sse("error", {"message": str(e)})
        return

    # 1) 存用户消息 + 拼装历史
    _save_message(storage, conv, "user", content=user_text)
    history = _load_history(storage, conv)

    # 2) ReAct 循环：工具决策（非流式）
    for _ in range(_MAX_TOOL_ROUNDS):
        message = llm_chat_with_tools(
            [{"role": "system", "content": _CHAT_SYSTEM}] + history,
            tools=TOOLS, temperature=0.3,
        )
        tool_calls = message.get("tool_calls") or []

        # 2a) 无工具调用 → 跳出循环，进入流式回复
        if not tool_calls:
            # 把这条 assistant 消息（可能含 content）也存入历史，供流式调用
            if message.get("content"):
                _save_message(storage, conv, "assistant",
                              content=message["content"])
                history.append({"role": "assistant", "content": message["content"]})
            break

        # 2b) 有工具调用 → 存 assistant 消息（含 tool_calls）+ 逐个执行
        _save_message(storage, conv, "assistant",
                      content=message.get("content", ""),
                      tool_calls=tool_calls)
        history.append({
            "role": "assistant", "content": message.get("content", ""),
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            tname = _tool_name(tc)
            args = _extract_args(tc)
            tc_id = _tool_call_id(tc)
            label = tool_label(tname, args)

            yield _sse("tool_start", {
                "tool_name": tname, "label": label, "args": args,
            })

            result = run_tool(rt, tname, args)
            # 若工具创建了计划，回填到 conversation（前端可据此跳转报告）
            if tname == "create_plan" and result.get("plan_id"):
                conv.plan_id = result["plan_id"]
                storage.put("conversations", conv.id, conv.model_dump())

            _save_message(storage, conv, "tool", content=json.dumps(
                result, ensure_ascii=False),
                tool_call_id=tc_id, tool_name=tname)
            history.append({
                "role": "tool", "tool_call_id": tc_id,
                "content": json.dumps(result, ensure_ascii=False),
            })

            # result_brief 给前端一个简短可读提示
            brief = _brief_result(tname, result)
            yield _sse("tool_end", {"tool_name": tname, "result_brief": brief})
        # 继续下一轮决策（LLM 看到工具结果后可能再调工具或回复）
    else:
        # 达到 _MAX_TOOL_ROUNDS 仍返回 tool_calls → 主动终止并提示
        yield _sse("token", {"text": "（已达到工具调用上限，下面直接给你结论）"})

    # 3) 流式回复（纯文本，无 tools）
    # 重新拉一次最新历史（上面的循环可能已追加了 assistant content）
    history = _load_history(storage, conv)
    collected = []
    try:
        for token in llm_chat_stream(
                [{"role": "system", "content": _CHAT_SYSTEM}] + history,
                temperature=0.5):
            collected.append(token)
            yield _sse("token", {"text": token})
    except Exception as e:
        logger.warning("流式回复失败: %s: %s", type(e).__name__, str(e)[:200])
        yield _sse("error", {"message": "回复生成失败：{}".format(str(e)[:200])})
        return

    full_reply = "".join(collected)
    if full_reply:
        _save_message(storage, conv, "assistant", content=full_reply)

    yield _sse("done", {
        "conversation_id": conv.id,
        "plan_id": conv.plan_id,
    })


def _brief_result(tool_name: str, result: Dict[str, Any]) -> str:
    """把工具结果浓缩成一句话，供前端'工具结束'气泡展示。"""
    if "error" in result:
        return "失败：" + str(result["error"])[:60]
    if tool_name == "search_routes":
        return "找到 {} 条线路".format(result.get("count", 0))
    if tool_name == "get_route_detail":
        return result.get("name", "") + " 详情已就绪"
    if tool_name == "parse_gear_list":
        return "识别出 {} 件装备".format(result.get("count", 0))
    if tool_name == "create_plan":
        danger = result.get("danger_count", 0)
        return "计划已创建（{} 项高危）".format(danger) if danger else "计划已创建 ✅"
    if tool_name == "check_weather_now":
        return "{} 项提醒".format(result.get("alert_count", 0))
    return "完成"

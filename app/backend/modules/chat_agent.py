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
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional

from models import Conversation, Message
from modules.agent_tools import TOOLS, ToolRuntime, run_tool, tool_label
from modules.gear import llm_chat_stream, llm_chat_with_tools, require_llm

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 5    # 单次对话最多工具调用轮数，防失控


_CHAT_SYSTEM = """你是「行山对账」的户外出行规划助手，专长是帮用户规划徒步和越野跑出行。

今天是 {today}。涉及“现在/最近/下个月”等时间判断一律以这个日期为准，
不要凭训练数据猜季节。

你是用户接触线路的唯一入口（页面没有线路列表），所以线路推荐、线路详情
都要通过你展示：用 search_routes / get_route_detail 拿到的结果会自动在对话里
渲染成线路卡片（含海拔剖面图），你的文字只需补充卡片之外的判断与建议，
不用重复复述距离/爬升等卡片已展示的数字。

用户可以在对话里直接发送：
- 轨迹文件（GPX / KML / KMZ）：系统会自动导入为线路并在消息里告诉你 route_id，
  直接用这个 id 调 get_route_detail 确认点位，再继续聊日期和装备。
- 图片（你能直接看到图片内容）：
  · 装备照片/装备清单截图 → 先用文字列出你识别到的品牌型号，
    再用 parse_gear_list 解析参数（把识别出的型号拼成 raw_text）；
  · 线路图/行程截图 → 提取地名、里程、爬升等关键信息，用 search_routes
    找线路库里最接近的线路；没有匹配就建议用户发 GPX/KML/KMZ 轨迹文件。

你的工作方式（按需调用工具，不要一次性问完所有信息）：
1. 先了解用户想去哪、什么活动（多日徒步还是越野跑）、什么时间。
2. 用 search_routes 推荐合适的线路，结合用户体能给建议。
3. 聊装备：问用户有什么或打算带什么，必要时用 parse_gear_list 帮忙识别参数。
4. 线路和日期确定、用户同意后，用 create_plan 创建计划——这会自动生成首份天气对账。
   装备没想好也可以先建（gear_raw_text 传空），提醒用户之后在对话或报告页补装备。
5. 用户想看天气就调 check_weather_now。

沟通风格：
- 全程用中文回复；确需英文术语时放在中文后的括号里，如“能见度（visibility）”，
  不要中英文夹杂。
- 你只以助手身份说话：绝不替用户拟台词、不虚构用户没说过的话，
  也不要在回复里演示“用户会怎么说”。
- 像懂户外的朋友聊天，专业但不啰嗦。一次只问 1-2 个问题，别列清单。
- 推荐线路时说清楚为什么适合（难度/季节/景观），给 2-3 个选择。
- 严格区分两类活动的装备逻辑：
  · 多日徒步：要宿营，睡袋/帐篷/炉具是核心，关注睡袋温标 vs 营地夜温、帐篷抗风
  · 越野跑：当日完赛不过夜，不需要睡袋帐篷（用户列了要提醒精简）；
    重点是强制装备：救生毯、头灯、防水外套、保暖层、软水壶/水袋、电解质
- 聊装备时尽量给可量化的性能要求，不要只说装备名：保暖看温标/蓬松度/充绒量，
  防水看静水压（mm），透气看 g/m²/24h，鞋看齿深与鞋面快干，贴身层强调速干/羊毛禁纯棉。
- parse_gear_list 结果里 needs_confirm=true 的装备，参数只是估计值：
  先把估计值报给用户并追问 missing_params 里的关键参数（如“睡袋舒适温标多少”），
  用户给出后把数值写进装备描述再调 create_plan；不要用估计参数直接建计划。
- 装备聊到关键缺失（如多日徒步无睡袋）要点出来，但别像查账。
- 天气有风险时要明确提示严重度，给出具体建议（装备/改期/换路线）。
- 不要编造未通过工具获取的数据。线路和天气信息必须来自工具结果。
- 搜线路结果为 0 时不要换参数反复重试，直接告诉用户没找到，
  并从工具返回的全部线路里挑最接近的推荐。

当用户用一句话就能回答时，直接回答，不必每次都调工具。"""


_WEEKDAYS = "一二三四五六日"


def _system_prompt() -> str:
    """每次调用时注入当前日期，避免模型凭训练数据猜季节。"""
    now = datetime.now()
    return _CHAT_SYSTEM.format(
        today="{} 星期{}".format(now.strftime("%Y-%m-%d"), _WEEKDAYS[now.weekday()]))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sse(event_type: str, data: Dict[str, Any]) -> str:
    """构造一条 SSE 事件。前端按 event 字段分发渲染。"""
    return "event: {}\ndata: {}\n\n".format(event_type, json.dumps(data, ensure_ascii=False))


# ── 消息历史存取 ──────────────────────────────────────────────────

def _load_history(storage, conv: Conversation) -> List[Dict[str, Any]]:
    """从 storage 读出对话的 OpenAI 格式消息历史。
    带图片的用户消息转成多模态 content（text + image_url）供 VLM 识别。"""
    history: List[Dict[str, Any]] = []
    for mid in conv.messages:
        m_data = storage.get("messages", mid)
        if not m_data:
            continue
        m = Message(**m_data)
        msg: Dict[str, Any] = {"role": m.role}
        if m.role == "user" and m.images:
            parts: List[Dict[str, Any]] = [
                {"type": "image_url", "image_url": {"url": u}} for u in m.images]
            parts.append({"type": "text", "text": m.content or "（见图片）"})
            msg["content"] = parts
        elif m.content:
            msg["content"] = m.content
        if m.tool_calls:
            msg["tool_calls"] = m.tool_calls
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        history.append(msg)
    return history


def _save_message(storage, conv: Conversation, role: str, content: str = "",
                  tool_calls: Optional[List[Dict]] = None,
                  tool_call_id: str = "", tool_name: str = "",
                  images: Optional[List[str]] = None) -> Message:
    """落库一条消息并挂到对话上。"""
    msg = Message(
        id=uuid.uuid4().hex[:10], conversation_id=conv.id, role=role,
        content=content, tool_calls=tool_calls or [],
        tool_call_id=tool_call_id, tool_name=tool_name,
        images=images or [], created_at=_now(),
    )
    storage.put("messages", msg.id, msg.model_dump())
    conv.messages.append(msg.id)
    conv.updated_at = _now()
    storage.put("conversations", conv.id, conv.model_dump())
    return msg


def _strip_images(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把多模态消息降级为纯文本。
    图片格式/体积不被模型接受时（典型如 HEIC → 400）兼容重试用，
    避免一张坏图毒化整个对话。"""
    out = []
    for m in history:
        if isinstance(m.get("content"), list):
            texts = [p.get("text", "") for p in m["content"] if p.get("type") == "text"]
            m = dict(m, content="[用户发了图片，但格式无法识别] " + " ".join(texts))
        out.append(m)
    return out


def _friendly_llm_error(e: Exception, prefix: str) -> str:
    """把 LLM 异常转成用户能看懂的提示（非限流类错误才会走到这）。"""
    return "{}：{}".format(prefix, str(e)[:160])


# ── 限流排队：全链降级都挤不进去时，不报错，后台排队重试到模型恢复 ──

_RETRY_WAITS = [5, 10, 20, 30, 30]   # 每轮排队等待秒数，总预算约 95s

_WAIT_FIRST_MSG = ("模型这会儿有点挤，我在后台排队重试，一有空位就自动接着回复——"
                   "不用重发消息，你可以先想想还有什么想问的。")
_WAIT_AGAIN_MSG = "还在排队中，再给我一点时间…"
_WAIT_FAIL_MSG = ("排了一会儿队模型还是很挤，这条消息我先放一放。"
                  "过一两分钟再发一次就好，或者先聊点别的。")


def _is_rate_limited(e: Exception) -> bool:
    s = str(e)
    return "429" in s or "503" in s or "quota" in s.lower()


def _queue_wait(wait_s: int, first: bool) -> Iterator[str]:
    """排队等待：先发柔和提示（notice 事件，前端渲染成灰色气泡而非报错），
    再分片 sleep，片间 yield SSE 注释保活连接。"""
    yield _sse("notice", {"message": _WAIT_FIRST_MSG if first else _WAIT_AGAIN_MSG})
    remain = wait_s
    while remain > 0:
        time.sleep(min(remain, 5))
        remain -= 5
        yield ": queue-wait\n\n"   # SSE 注释行，前端解析时自动忽略


def _call_queued(call: Callable[[], Any], label: str) -> Iterator[str]:
    """非流式 LLM 调用带限流排队：作为生成器 yield SSE 事件，
    结果通过 return 返回（调用方用 `yield from` 接）。非限流错误直接抛。"""
    attempt = 0
    while True:
        try:
            return call()
        except Exception as e:
            if not _is_rate_limited(e) or attempt >= len(_RETRY_WAITS):
                raise
            logger.warning("%s 全链限流，排队 %ss 后重试（第 %s/%s 次）",
                           label, _RETRY_WAITS[attempt], attempt + 1, len(_RETRY_WAITS))
            yield from _queue_wait(_RETRY_WAITS[attempt], attempt == 0)
            attempt += 1


def _soft_giveup(e: Exception, conv: Conversation) -> Iterator[str]:
    """排队预算耗尽后的收尾：限流类失败不给用户看“调用失败”，
    只发柔和 notice + done；非限流的真异常才走 error 事件。"""
    if _is_rate_limited(e):
        yield _sse("notice", {"message": _WAIT_FAIL_MSG})
        yield _sse("done", {"conversation_id": conv.id, "plan_id": conv.plan_id})
    else:
        yield _sse("error", {"message": _friendly_llm_error(e, "出了点问题")})


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
             rt: ToolRuntime, images: Optional[List[str]] = None) -> Iterator[str]:
    """处理一条用户消息（可附图片），yield SSE 事件流。

    前端收到的 event 类型：
      tool_start {tool_name, label, args}   —— 工具开始（显示"正在…")
      tool_end   {tool_name, result_brief, routes?} —— 工具结束（线路类工具附卡片数据）
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
    _save_message(storage, conv, "user", content=user_text, images=images)
    history = _load_history(storage, conv)

    # 2) ReAct 循环：工具决策（非流式）
    # 异常必须兜住转成 error 事件：否则 SSE 流直接断掉，前端只能看到 network error
    system_prompt = _system_prompt()
    strip_images = False    # 带图请求被拒后置 True，后续阶段统一去图
    for _ in range(_MAX_TOOL_ROUNDS):
        def _decide():
            return llm_chat_with_tools(
                [{"role": "system", "content": system_prompt}] + history,
                tools=TOOLS, temperature=0.3,
            )
        try:
            # 限流时在 _call_queued 里排队重试（期间 yield notice 事件），
            # 排队预算耗尽才会抛到这里
            message = yield from _call_queued(_decide, "工具决策")
        except Exception as e:
            stripped = _strip_images(history)
            if stripped != history and not _is_rate_limited(e):
                # 典型场景：HEIC/超大图 → 400，剥离图片降级重试一次
                logger.warning("带图请求失败(%s)，剥离图片重试: %s",
                               type(e).__name__, str(e)[:120])
                history = stripped
                strip_images = True
                try:
                    message = yield from _call_queued(_decide, "工具决策(去图)")
                except Exception as e2:
                    logger.warning("工具决策失败: %s: %s", type(e2).__name__, str(e2)[:200])
                    yield from _soft_giveup(e2, conv)
                    return
            else:
                logger.warning("工具决策失败: %s: %s", type(e).__name__, str(e)[:200])
                yield from _soft_giveup(e, conv)
                return
        tool_calls = message.get("tool_calls") or []

        # 2a) 无工具调用 → 丢弃决策阶段的草稿，由下方流式调用统一生成回复。
        # （若把草稿存进历史再流式续写，模型会把对话“接着往下演”，替用户说话）
        if not tool_calls:
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

            # result_brief 给前端一个简短可读提示；线路类工具附上卡片数据
            brief = _brief_result(tname, result)
            payload: Dict[str, Any] = {"tool_name": tname, "result_brief": brief}
            routes_data = _routes_for_card(tname, result)
            if routes_data:
                payload["routes"] = routes_data
            yield _sse("tool_end", payload)
        # 继续下一轮决策（LLM 看到工具结果后可能再调工具或回复）
    else:
        # 达到 _MAX_TOOL_ROUNDS 仍返回 tool_calls → 主动终止并提示
        yield _sse("token", {"text": "（已达到工具调用上限，下面直接给你结论）"})

    # 3) 流式回复（纯文本，无 tools）
    # 重新拉一次最新历史（上面的循环可能已追加工具消息）
    history = _load_history(storage, conv)
    if strip_images:
        history = _strip_images(history)
    collected = []
    attempt = 0
    while True:
        try:
            for token in llm_chat_stream(
                    [{"role": "system", "content": system_prompt}] + history,
                    temperature=0.5):
                collected.append(token)
                yield _sse("token", {"text": token})
            break
        except Exception as e:
            logger.warning("流式回复失败: %s: %s", type(e).__name__, str(e)[:200])
            if collected:
                # token 已发给用户，中途断流不重跑：保留已生成部分直接收尾
                break
            if _is_rate_limited(e) and attempt < len(_RETRY_WAITS):
                yield from _queue_wait(_RETRY_WAITS[attempt], attempt == 0)
                attempt += 1
                continue
            yield from _soft_giveup(e, conv)
            return

    full_reply = "".join(collected)
    if full_reply:
        _save_message(storage, conv, "assistant", content=full_reply)

    yield _sse("done", {
        "conversation_id": conv.id,
        "plan_id": conv.plan_id,
    })


def _routes_for_card(tool_name: str, result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """线路类工具结果 → 前端对话内线路卡片数据（含 waypoints 供剖面图）。"""
    if "error" in result:
        return []
    if tool_name == "get_route_detail":
        return [result] if result.get("waypoints") else []
    if tool_name == "search_routes":
        # search 结果是精简字段（无 waypoints），前端用本地线路表补全剖面
        return result.get("routes") or []
    return []


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

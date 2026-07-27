"""装备模块：用户自由输入装备名/清单 → 结构化 GearItem。

解析链（数据库优先，LLM 只做预填）：
1. 装备数据库（gear_db：预置库 + 用户回写库）：命中即用库内参数，无需确认
2. 未命中 → LLM 估参预填（可选 Tavily 搜索增强），标记 needs_review=True，
   由用户在前端确认/修改后生效，并回写 gear_db_user 供下次直接命中
3. LLM 不可用 → 空参数 + needs_review=True（纯手填兜底）
"""
import json
import logging
import re
import time
from typing import Any, Dict, Iterator, List, Optional

import httpx

from config import (MODELSCOPE_API_KEY, MODELSCOPE_BASE_URL, LLM_MODEL,
                    LLM_MODEL_CHAIN, TAVILY_API_KEY)
from models import GearItem
from modules import gear_db

logger = logging.getLogger(__name__)

# 主模型 429/配额耗尽时依次换用的备用模型（含主模型本身）
_MODEL_CHAIN = LLM_MODEL_CHAIN or [LLM_MODEL]


def _is_vl_model(model: str) -> bool:
    """VL 模型才能吃多模态 content（image_url）；纯文本模型需先剥图。"""
    return "-vl-" in model.lower()


def _strip_image_parts(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把多模态 content 降级成纯文本，供非 VL 备用模型使用。"""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            texts = [p.get("text", "") for p in c if p.get("type") == "text"]
            m = dict(m, content="[用户发了图片，当前模型不支持读图] " + " ".join(texts))
        out.append(m)
    return out


def _should_fallback(exc: Exception) -> bool:
    """仅在限流/配额类错误上降级换模型；其它错误（如网络、鉴权）直接上抛。"""
    resp = getattr(exc, "response", None)
    if resp is not None and resp.status_code in (429, 503):
        return True
    return False

_CAT_GUESS = [
    (r"睡袋", "sleep"), (r"帐篷|天幕", "shelter"),
    (r"冲锋衣|雨|防水", "rain"), (r"羽绒|抓绒|保暖|棉服", "warm"),
    (r"鞋|靴", "footwear"),
]


def _guess_category(name: str) -> str:
    for pattern, cat in _CAT_GUESS:
        if re.search(pattern, name):
            return cat
    return "other"


# ── 联网搜索（Tavily，可选）─────────────────────────────────────

def _web_search(query: str) -> str:
    if not TAVILY_API_KEY:
        return ""
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": TAVILY_API_KEY, "query": query,
                  "max_results": 3, "search_depth": "basic"},
            timeout=12,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return "\n".join(r.get("content", "")[:400] for r in results)
    except Exception as e:
        logger.warning("Tavily 搜索失败，降级为无搜索上下文: %s: %s", type(e).__name__, str(e)[:200])
        return ""


# ── LLM 调用（魔搭 API-Inference，OpenAI 兼容）──────────────────

# 免费 key 按模型限 QPS：工具决策与流式回复背靠背发出必 429，
# 所有 LLM 请求共享一个最小间隔闸门
_MIN_LLM_GAP_S = 1.1
_last_llm_ts = 0.0


def _mark_llm_ts() -> None:
    global _last_llm_ts
    _last_llm_ts = time.time()


def _throttle() -> None:
    """确保距上一次请求结束至少间隔 _MIN_LLM_GAP_S 再发下一次。"""
    wait = _last_llm_ts + _MIN_LLM_GAP_S - time.time()
    if wait > 0:
        time.sleep(wait)
    _mark_llm_ts()


def _llm_chat(system: str, user: str) -> str:
    if not MODELSCOPE_API_KEY:
        return ""
    try:
        msg = llm_chat_with_tools(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            tools=None, temperature=0.2)
        return msg.get("content", "") or ""
    except Exception as e:
        logger.warning("LLM 调用失败，降级为内置知识库: %s: %s", type(e).__name__, str(e)[:200])
        return ""


def require_llm() -> None:
    """对话/工具调用等强依赖 LLM 的入口检查。无 Key 直接抛错，不静默降级。"""
    if not MODELSCOPE_API_KEY:
        raise RuntimeError("未配置 MODELSCOPE_API_KEY，对话功能不可用")


def _build_payload(messages: List[Dict[str, Any]],
                   tools: Optional[List[Dict[str, Any]]] = None,
                   temperature: float = 0.3,
                   model: str = "") -> Dict[str, Any]:
    """构造 OpenAI 兼容请求体（messages 已是完整对话历史，含 system/user/assistant/tool）。"""
    payload: Dict[str, Any] = {
        "model": model or LLM_MODEL, "temperature": temperature, "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def llm_chat_with_tools(messages: List[Dict[str, Any]],
                        tools: Optional[List[Dict[str, Any]]] = None,
                        temperature: float = 0.3) -> Dict[str, Any]:
    """非流式 + function calling：返回完整 message（含 content 或 tool_calls）。

    用于对话 Agent 的工具决策阶段——魔搭 OpenAI 兼容接口在 stream+tools 组合下
    解析 tool_calls 易出错，故工具决策一律走非流式。
    主模型 429/配额耗尽时沿 _MODEL_CHAIN 依次降级；降到非 VL 模型时自动剥图。
    """
    require_llm()
    last_exc: Optional[Exception] = None
    for i, model in enumerate(_MODEL_CHAIN):
        msgs = messages if _is_vl_model(model) else _strip_image_parts(messages)
        _throttle()
        try:
            resp = httpx.post(
                MODELSCOPE_BASE_URL + "/chat/completions",
                headers={"Authorization": "Bearer " + MODELSCOPE_API_KEY},
                json=_build_payload(msgs, tools, temperature, model),
                timeout=60,
            )
            resp.raise_for_status()
            if i > 0:
                logger.info("LLM 已降级到备用模型: %s", model)
            return resp.json()["choices"][0]["message"]
        except Exception as e:
            if _should_fallback(e) and i < len(_MODEL_CHAIN) - 1:
                logger.warning("模型 %s 限流/配额耗尽，降级到 %s", model, _MODEL_CHAIN[i + 1])
                last_exc = e
                continue
            raise
        finally:
            _mark_llm_ts()   # 以请求结束时刻计间隔，防长请求后背靠背再发
    raise last_exc  # 链上全部限流，抛最后一个错误


def llm_chat_stream(messages: List[Dict[str, Any]],
                    temperature: float = 0.5) -> Iterator[str]:
    """纯文本流式生成（不带 tools）：逐 token yield 文本片段。

    用于把对话 Agent 的最终回复推给前端打字机渲染。
    OpenAI 兼容 SSE：data: {chunk}\\n\\n，末尾 data: [DONE]。
    降级策略同 llm_chat_with_tools；一旦开始吐 token 就不再切模型。
    """
    require_llm()
    last_exc: Optional[Exception] = None
    for i, model in enumerate(_MODEL_CHAIN):
        msgs = messages if _is_vl_model(model) else _strip_image_parts(messages)
        payload = _build_payload(msgs, tools=None, temperature=temperature, model=model)
        payload["stream"] = True
        _throttle()
        try:
            with httpx.stream(
                "POST", MODELSCOPE_BASE_URL + "/chat/completions",
                headers={"Authorization": "Bearer " + MODELSCOPE_API_KEY},
                json=payload, timeout=120,
            ) as resp:
                resp.raise_for_status()   # 失败发生在首 token 前，此处可安全降级
                if i > 0:
                    logger.info("LLM 流式已降级到备用模型: %s", model)
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}).get("content")
                    if delta:
                        yield delta
                return
        except Exception as e:
            if _should_fallback(e) and i < len(_MODEL_CHAIN) - 1:
                logger.warning("流式模型 %s 限流/配额耗尽，降级到 %s", model, _MODEL_CHAIN[i + 1])
                last_exc = e
                continue
            raise
        finally:
            _mark_llm_ts()
    raise last_exc


_PARSE_SYSTEM = """你是户外装备专家。把用户的装备清单解析为 JSON 数组，每项：
{"name":"原始名称","category":"sleep|shelter|rain|warm|footwear|other",
"params":{睡袋给 comfort_c/limit_c，帐篷给 type/wind_ms，防水给 waterproof_mm，保暖给 rating_c},
"confidence":"high|medium|low","note":"一句话说明"}
若提供了搜索资料，优先用资料中的真实参数（confidence=high）；否则用你的知识估计（confidence=medium）。
只输出 JSON 数组，不要其他文字。"""


def _llm_parse(raw_text: str, search_context: str) -> List[GearItem]:
    user = "装备清单：\n" + raw_text
    if search_context:
        user += "\n\n搜索资料：\n" + search_context
    content = _llm_chat(_PARSE_SYSTEM, user)
    if not content:
        return []
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        out.append(GearItem(
            name=str(it["name"]),
            category=it.get("category", "other"),
            params=it.get("params", {}) or {},
            param_source="web_search" if search_context else "llm_estimate",
            confidence=it.get("confidence", "medium"),
            note=it.get("note", ""),
        ))
    return out


# 占位词不是装备：对话建计划时 LLM 常传入“暂无装备”之类文本，直接过滤
_PLACEHOLDER = re.compile(
    r"^(暂无.*|无|没有.*|待定|待补充|不清楚|未知|无装备|none|n/?a|-+|\?+|？+)$",
    re.IGNORECASE)


def _split_lines(raw_text: str) -> List[str]:
    parts = re.split(r"[\n,，、;；]+", raw_text)
    return [p.strip() for p in parts
            if p.strip() and not _PLACEHOLDER.match(p.strip())]


def parse_gear(raw_text: str) -> List[GearItem]:
    """主入口：装备数据库优先，未命中的由 LLM 预填估计值待用户确认。"""
    names = _split_lines(raw_text)
    if not names:
        return []

    # 1) 逐件匹配装备库（用户回写库优先）
    items: List[Optional[GearItem]] = []
    unmatched: List[str] = []
    for n in names:
        entry = gear_db.match(n)
        if entry:
            items.append(GearItem(
                name=n, category=entry["category"], params=dict(entry["params"]),
                param_source=entry.get("source", "gear_db"), confidence="high",
                needs_review=False, note=entry.get("name", "")))
        else:
            items.append(None)
            unmatched.append(n)

    # 2) 未命中条目：LLM 估参预填（可选搜索增强），等用户确认
    est_by_name: Dict[str, GearItem] = {}
    est_ordered: List[GearItem] = []
    search_ctx = ""
    if unmatched:
        if TAVILY_API_KEY:
            # 只搜前 5 件，控制延迟与额度
            for n in unmatched[:5]:
                search_ctx += _web_search(n + " 参数 温标 防水指数 户外装备")
        est_ordered = _llm_parse("\n".join(unmatched), search_ctx)
        est_by_name = {e.name.strip().lower(): e for e in est_ordered}

    # 3) 回填：名称对得上用名称，对不上按顺序对齐（LLM 按输入行序输出）
    for idx, n in enumerate(unmatched):
        est = est_by_name.get(n.strip().lower())
        if est is None and idx < len(est_ordered):
            est = est_ordered[idx]
        pos = items.index(None)
        if est:
            items[pos] = GearItem(
                name=n, category=est.category, params=est.params,
                param_source="web_search" if search_ctx else "llm_estimate",
                confidence="medium", needs_review=True,
                note=est.note or "AI 估计参数，请确认")
        else:
            items[pos] = GearItem(
                name=n, category=_guess_category(n), params={},
                param_source="unknown", confidence="low",
                needs_review=True, note="未识别参数，请补齐")

    # 4) 必填参数缺失的一律转入待确认（含库命中但库内参数不全的情况）
    result = [it for it in items if it is not None]
    for it in result:
        if gear_db.missing_required(it.category, it.params):
            it.needs_review = True
    return result

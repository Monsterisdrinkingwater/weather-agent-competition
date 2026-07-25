"""装备模块：用户自由输入装备名/清单 → 结构化 GearItem。

解析链（逐级降级，任何一级失败不阻塞）：
1. LLM + 联网搜索：Tavily 搜装备参数，LLM 抽取结构化字段（confidence=high）
2. 纯 LLM 估参：无搜索 Key 或搜索失败时，LLM 用内置知识估计（confidence=medium）
3. 内置知识库：无 LLM 时，关键词规则匹配常见装备（confidence=low~high）
"""
import json
import re
from typing import List, Optional

import httpx

from config import MODELSCOPE_API_KEY, MODELSCOPE_BASE_URL, LLM_MODEL, TAVILY_API_KEY
from models import GearItem

# ── 内置知识库：常见国产/主流户外装备（兜底 + 演示保障）──────────
_BUILTIN = [
    # 匹配关键词(小写), 类别, 参数, 置信度, 备注
    (r"黑冰.*b1500|b1500", "sleep", {"comfort_c": -12, "limit_c": -18, "fill": "700蓬鹅绒", "weight_g": 1900}, "high", "黑冰 B1500 羽绒睡袋"),
    (r"黑冰.*b1000|b1000", "sleep", {"comfort_c": -5, "limit_c": -12, "fill": "700蓬鹅绒", "weight_g": 1450}, "high", "黑冰 B1000 羽绒睡袋"),
    (r"黑冰.*b700|b700", "sleep", {"comfort_c": 1, "limit_c": -5, "fill": "700蓬鹅绒", "weight_g": 1150}, "high", "黑冰 B700 羽绒睡袋"),
    (r"黑冰.*b400|b400", "sleep", {"comfort_c": 8, "limit_c": 3, "fill": "700蓬鹅绒", "weight_g": 800}, "high", "黑冰 B400 羽绒睡袋"),
    (r"信封|棉睡袋", "sleep", {"comfort_c": 10, "limit_c": 5}, "medium", "普通棉睡袋按温标 10°C 估"),
    (r"msr.*hubba|hubba", "shelter", {"type": "双层三季帐", "wind_ms": 15, "weight_g": 1720}, "high", "MSR Hubba Hubba NX 2"),
    (r"自由之魂|远行客|libra", "shelter", {"type": "双层三季帐", "wind_ms": 15}, "medium", "自由之魂系列三季帐"),
    (r"三峰|3f.*ul", "shelter", {"type": "双层三季帐", "wind_ms": 13}, "medium", "三峰出品三季帐"),
    (r"四季帐|高山帐", "shelter", {"type": "四季帐", "wind_ms": 22}, "medium", "四季/高山帐"),
    (r"始祖鸟.*beta|beta\s*(lt|ar|sl)", "rain", {"waterproof_mm": 28000, "membrane": "GORE-TEX"}, "high", "始祖鸟 Beta 系列硬壳"),
    (r"凯乐石.*hardshell|mont-x|filo", "rain", {"waterproof_mm": 20000, "membrane": "FILTEC"}, "medium", "凯乐石硬壳"),
    (r"冲锋衣|硬壳", "rain", {"waterproof_mm": 10000}, "low", "通用冲锋衣按 10000mm 估"),
    (r"雨衣|雨披", "rain", {"waterproof_mm": 8000}, "medium", "雨披类，防风性弱"),
    (r"皮肤衣|防晒衣", "rain", {"waterproof_mm": 0}, "high", "皮肤衣不防水"),
    (r"羽绒服|排骨羽绒", "warm", {"insulation": "down", "rating_c": -5}, "medium", "轻量羽绒保暖层"),
    (r"抓绒", "warm", {"insulation": "fleece", "rating_c": 8}, "medium", "抓绒中间层"),
    (r"棉服|化纤棉", "warm", {"insulation": "synthetic", "rating_c": 0}, "medium", "化纤棉保暖层"),
    (r"越野跑鞋|hoka|speedgoat|萨洛蒙|salomon|凯乐石.*fuga|fuga", "footwear", {"type": "越野跑鞋", "grip": "湿滑路面注意"}, "medium", "越野跑鞋"),
    (r"登山鞋|徒步鞋|重装鞋", "footwear", {"type": "徒步鞋", "waterproof": True}, "medium", "徒步鞋"),
    (r"雪套", "other", {"use": "防雪防泥"}, "high", "雪套"),
    (r"冰爪", "other", {"use": "冰雪路面"}, "high", "冰爪"),
    (r"登山杖|手杖", "other", {"use": "省力/防滑"}, "high", "登山杖"),
    (r"头灯", "other", {"use": "夜间行进必备"}, "high", "头灯"),
    (r"救生毯|急救毯", "other", {"use": "失温应急"}, "high", "救生毯"),
    (r"炉头|气罐|套锅", "other", {"use": "热食热饮，低温关键"}, "high", "炊具"),
]

_CAT_GUESS = [
    (r"睡袋", "sleep"), (r"帐篷|天幕", "shelter"),
    (r"冲锋衣|雨|防水", "rain"), (r"羽绒|抓绒|保暖|棉服", "warm"),
    (r"鞋|靴", "footwear"),
]


def _builtin_lookup(name: str) -> Optional[GearItem]:
    low = name.lower()
    for pattern, cat, params, conf, note in _BUILTIN:
        if re.search(pattern, low):
            return GearItem(name=name, category=cat, params=params,
                            param_source="builtin", confidence=conf, note=note)
    return None


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
    except Exception:
        return ""


# ── LLM 调用（魔搭 API-Inference，OpenAI 兼容）──────────────────

def _llm_chat(system: str, user: str) -> str:
    if not MODELSCOPE_API_KEY:
        return ""
    try:
        resp = httpx.post(
            MODELSCOPE_BASE_URL + "/chat/completions",
            headers={"Authorization": "Bearer " + MODELSCOPE_API_KEY},
            json={"model": LLM_MODEL, "temperature": 0.2,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return ""


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


def _split_lines(raw_text: str) -> List[str]:
    parts = re.split(r"[\n,，、;；]+", raw_text)
    return [p.strip() for p in parts if p.strip()]


def parse_gear(raw_text: str) -> List[GearItem]:
    """主入口：三级降级解析。"""
    names = _split_lines(raw_text)
    if not names:
        return []

    # 1) LLM（带搜索上下文则参数更准）
    search_ctx = ""
    if TAVILY_API_KEY:
        # 只搜前 5 件，控制延迟与额度
        for n in names[:5]:
            search_ctx += _web_search(n + " 参数 温标 防水指数 户外装备")
    llm_items = _llm_parse(raw_text, search_ctx)
    if llm_items:
        # 内置库校准：命中内置库且 LLM 置信度低的，以内置参数为准
        for item in llm_items:
            if item.confidence != "high":
                hit = _builtin_lookup(item.name)
                if hit:
                    item.params, item.param_source = hit.params, "builtin"
                    item.confidence, item.note = hit.confidence, hit.note
        return llm_items

    # 2) 纯内置知识库 + 类别猜测（无 LLM 环境的演示兜底）
    out = []
    for n in names:
        hit = _builtin_lookup(n)
        if hit:
            out.append(hit)
        else:
            out.append(GearItem(
                name=n, category=_guess_category(n), params={},
                param_source="unknown", confidence="low",
                note="未识别参数，建议手动确认",
            ))
    return out

"""装备数据库：服务器预置库 + 用户确认回写的自定义条目。

数据来源两层，匹配时用户条目优先（用户确认过的参数最可信）：
1. data/gear_db.json —— 预置库，约 50 条主流装备的公开标称参数
2. storage 集合 gear_db_user —— 用户在补参界面确认后的回写条目

条目结构：{id, name, match(正则), category, params, source}
"""
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import storage

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gear_db.json"

# 按类别定义"给建议前必须有值"的参数（对账引擎实际消费的字段）
REQUIRED_PARAMS: Dict[str, List[str]] = {
    "sleep": ["comfort_c"],
    "rain": ["waterproof_mm"],
    "shelter": ["wind_ms"],
    "warm": ["rating_c"],
}

_preset_cache: Optional[List[Dict[str, Any]]] = None


def _load_preset() -> List[Dict[str, Any]]:
    global _preset_cache
    if _preset_cache is None:
        try:
            data = json.loads(_DB_PATH.read_text(encoding="utf-8"))
            _preset_cache = data.get("entries", [])
        except Exception as e:
            logger.warning("装备库加载失败，退化为空库: %s: %s", type(e).__name__, str(e)[:200])
            _preset_cache = []
    return _preset_cache


def load() -> List[Dict[str, Any]]:
    """全量装备库：用户回写条目在前（匹配优先），预置库在后。"""
    user_entries = []
    for e in storage.all_values("gear_db_user"):
        e = dict(e)
        e["source"] = "user"
        user_entries.append(e)
    preset = [dict(e, source="gear_db") for e in _load_preset()]
    return user_entries + preset


def match(name: str) -> Optional[Dict[str, Any]]:
    """按名称匹配装备库条目，未命中返回 None。"""
    low = name.lower().strip()
    if not low:
        return None
    for entry in load():
        pattern = entry.get("match") or ""
        if not pattern:
            continue
        try:
            if re.search(pattern, low, re.IGNORECASE):
                return entry
        except re.error:
            continue
    return None


def missing_required(category: str, params: Dict[str, Any]) -> List[str]:
    """该类别下建议引擎必需但缺失的参数名列表。"""
    return [k for k in REQUIRED_PARAMS.get(category, [])
            if params.get(k) is None]


def save_user_entry(name: str, category: str, params: Dict[str, Any],
                    note: str = "") -> Dict[str, Any]:
    """用户确认参数后回写：同名装备下次解析直接命中。"""
    key = name.lower().strip()
    # 同名覆盖（用户修正参数时不产生重复条目）
    existing_id = None
    for e in storage.all_values("gear_db_user"):
        if e.get("name", "").lower().strip() == key:
            existing_id = e.get("id")
            break
    entry = {
        "id": existing_id or uuid.uuid4().hex[:10],
        "name": name.strip(),
        "match": re.escape(key),
        "category": category,
        "params": params,
        "note": note or "用户确认参数",
    }
    storage.put("gear_db_user", entry["id"], entry)
    return dict(entry, source="user")

"""轻量 JSON 文件存储：MVP 无账号体系，单文件按集合存取。"""
import json
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional

from config import STORE_DIR

_lock = threading.Lock()


def _path(collection: str) -> Path:
    return STORE_DIR / (collection + ".json")


def _load(collection: str) -> Dict[str, Any]:
    p = _path(collection)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _dump(collection: str, data: Dict[str, Any]) -> None:
    tmp = _path(collection).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_path(collection))


def get(collection: str, key: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _load(collection).get(key)


def put(collection: str, key: str, value: Dict[str, Any]) -> None:
    with _lock:
        data = _load(collection)
        data[key] = value
        _dump(collection, data)


def all_values(collection: str) -> List[Dict[str, Any]]:
    with _lock:
        return list(_load(collection).values())


def delete(collection: str, key: str) -> bool:
    with _lock:
        data = _load(collection)
        if key in data:
            del data[key]
            _dump(collection, data)
            return True
        return False

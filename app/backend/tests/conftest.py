"""让 tests/ 下的用例能直接 import backend 顶层模块（config/models/modules）。"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

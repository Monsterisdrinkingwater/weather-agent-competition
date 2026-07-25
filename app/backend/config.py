"""全局配置：所有外部依赖走环境变量，缺失时自动降级到演示模式。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ROUTES_DIR = DATA_DIR / "routes"
STORE_DIR = DATA_DIR / "store"
STORE_DIR.mkdir(parents=True, exist_ok=True)

# ── 天机天气 API ──────────────────────────────────────────────
# 申请到 Key 后填入环境变量即自动切换真实数据源
TJ_API_KEY = os.environ.get("TJ_API_KEY", "")
TJ_API_BASE = os.environ.get("TJ_API_BASE", "https://api.tjweather.com")
WEATHER_DEMO_MODE = not TJ_API_KEY  # 无 Key → 确定性模拟数据

# ── LLM（魔搭 API-Inference，OpenAI 兼容协议）────────────────
MODELSCOPE_API_KEY = os.environ.get("MODELSCOPE_API_KEY", "")
MODELSCOPE_BASE_URL = os.environ.get(
    "MODELSCOPE_BASE_URL", "https://api-inference.modelscope.cn/v1"
)
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen2.5-72B-Instruct")

# ── 联网搜索（装备参数检索，可选）────────────────────────────
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# ── diff 引擎阈值（触发提醒事件的最小变化量）─────────────────
THRESHOLDS = {
    "t_min_drop": 3.0,      # 最低温下调 ≥3°C
    "t_max_rise": 4.0,      # 最高温上调 ≥4°C
    "precip_new_mm": 3.0,   # 新增降水 ≥3mm/日
    "precip_jump_mm": 8.0,  # 降水量增幅 ≥8mm/日
    "wind_jump_ms": 4.0,    # 风速增幅 ≥4m/s
    "wind_danger_ms": 13.9, # 达 7 级风即危险（无论增幅）
    "t_min_danger": -10.0,  # 低于此温度直接高危
}

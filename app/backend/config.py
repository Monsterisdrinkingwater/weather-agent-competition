"""全局配置：所有外部依赖走环境变量，缺失时自动降级到演示模式。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ROUTES_DIR = DATA_DIR / "routes"
STORE_DIR = DATA_DIR / "store"
STORE_DIR.mkdir(parents=True, exist_ok=True)

# ── 天机天气 API（新版 /v2，需 key + subscriptionId）──────────
# 申请到 Key 后填入环境变量即自动切换真实数据源
TJ_API_KEY = os.environ.get("TJ_API_KEY", "")
TJ_API_BASE = os.environ.get("TJ_API_BASE", "https://api.tjweather.com")
TJ_SUBSCRIPTION_ID = os.environ.get("TJ_SUBSCRIPTION_ID", "")
# v2 接口 key 与 subscriptionId 缺一不可，半配置也降级到演示模式
WEATHER_DEMO_MODE = not (TJ_API_KEY and TJ_SUBSCRIPTION_ID)

# ── LLM（魔搭 API-Inference，OpenAI 兼容协议）────────────────
MODELSCOPE_API_KEY = os.environ.get("MODELSCOPE_API_KEY", "")
MODELSCOPE_BASE_URL = os.environ.get(
    "MODELSCOPE_BASE_URL", "https://api-inference.modelscope.cn/v1"
)
# 默认用 Qwen3 视觉旗舰：支持图片识别（装备照片/线路截图直接发对话里），
# function calling 与流式实测均可用；免费 key 实测仅 Qwen 系可用，
# Qwen2.5-VL 系列无 provider，勿回退
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen3-VL-235B-A22B-Instruct")

# 魔搭免费 key 按模型分别计 QPS/每日额度：主模型 429 时依次降级。
# 降级链全部用 VL 模型，保证用户任何时候都能发图识图。
# 2026-07 实测：VL-235B / VL-30B / VL-8B 均可用；
# Qwen3-VL-32B、Qwen2.5-VL 系列无 provider 勿加入。
_FALLBACKS = os.environ.get(
    "LLM_FALLBACK_MODELS",
    "Qwen/Qwen3-VL-30B-A3B-Instruct,"
    "Qwen/Qwen3-VL-8B-Instruct",
)
LLM_MODEL_CHAIN = [LLM_MODEL] + [
    m.strip() for m in _FALLBACKS.split(",") if m.strip() and m.strip() != LLM_MODEL
]

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
    # —— 规划期风险分析（单快照即可，不依赖历史对比）——
    "t_range_big": 15.0,    # 昼夜温差 ≥15°C 提醒分层穿衣
    "trip_drop": 5.0,       # 行程内相邻两天最低温骤降 ≥5°C（冷空气过境）
    "t_heat": 32.0,         # 高温风险阈值（越野跑收紧 2°C）
}

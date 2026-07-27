# 天气智能创造营 — Weather Intelligence AI

> 🏆 中科天机 × 魔搭社区 联合举办
> 📍 赛道：行业应用 — 户外安全方向

---

## 📦 项目内容

| 文件 | 说明 |
|------|------|
| `市场调研报告.md` | 天气API市场、竞品分析、赛道对比 |
| `项目文档.md` | 创意库 + 产品定位讨论复盘 |
| `app/` | 「行山对账·户外计划助手」可运行原型 |

## 🏔️ 产品方向

**行山对账** — 天气数据 × 路线数据 × 装备数据 → 出行安全分析

用户选一条徒步路线 → 系统获取沿途天气预报 → 用户填写装备清单 → 三方交叉分析 → 输出安全提醒报告。

## 🚀 快速启动

```bash
cd app/backend
PYTHONPATH="" ../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000/`

> ⚠️ `PYTHONPATH=""` 是因为 Hermes 系统环境变量会污染项目 venv 的 Python 3.9。

## 🔑 环境变量（可选）

| 变量 | 作用 | 缺失时 |
|------|------|--------|
| `TJ_API_KEY` | 天机天气 API（新版 /v2） | 演示模拟数据 |
| `TJ_SUBSCRIPTION_ID` | 天机新版 API 订阅 ID | 演示模拟数据 |
| `MODELSCOPE_API_KEY` | LLM 报告生成 | 模板兜底 |
| `TAVILY_API_KEY` | 联网搜索装备参数 | 内置知识库兜底 |

## 📊 技术栈

- **后端**：Python FastAPI + uvicorn
- **前端**：原生 HTML/CSS/JS（无框架）
- **天气数据**：中科天机 API / 演示模拟
- **AI**：魔搭 API-Inference（OpenAI 兼容）

## 🏁 参赛情况

- ✅ 天气智能创造营（中科天机 × 魔搭）— 报名完成
- ✅ 外滩黑客松·AI Coding大赛 — 7/20~8/9

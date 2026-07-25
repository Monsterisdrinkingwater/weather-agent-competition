# AGENTS.md

面向 AI 编码助手与协作者的项目速览。所有命令均已实际执行验证（macOS / zsh / Python 3.9.6）。

## 项目结构

本仓库包含两部分内容：

```
.
├── README.md                 # 比赛信息 + API 技术分析 + 调研摘要
├── 市场调研报告.md            # 调研文档
├── 选题创意库_212个.md        # 调研文档
├── 项目复盘_产品定位讨论.md    # 调研文档
├── 项目立项汇报.md            # 调研文档
└── app/                      # 「户外计划助手」可运行原型
    ├── requirements.txt      # Python 依赖（FastAPI + uvicorn 等）
    ├── backend/              # FastAPI 后端
    │   ├── main.py           # 入口，含全部 API 路由（见文件顶部注释）
    │   ├── config.py         # 环境变量与演示模式开关
    │   ├── models.py / storage.py
    │   ├── modules/          # weather / diff_engine / agent / gear / gpx
    │   └── data/routes/      # 6 条预置线路 JSON
    └── frontend/             # 纯静态前端（由后端 / 路径直接托管）
```

## 安装

```bash
# 首次：创建虚拟环境（仓库内已有 app/.venv 可直接复用）
python3 -m venv app/.venv

# 激活并安装依赖
source app/.venv/bin/activate
pip install -r app/requirements.txt
```

## 启动

⚠️ **注意**：Hermes 系统环境变量 `PYTHONPATH` 指向 Python 3.11 包，与项目 venv 的 3.9 冲突，启动时必须清除。

```bash
cd app/backend
PYTHONPATH="" ../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

默认监听 `http://127.0.0.1:8000`；端口被占用时加 `--port 8001`。

验证：

```bash
curl http://127.0.0.1:8000/api/meta
```

验证服务可用：

```bash
curl http://127.0.0.1:8000/api/meta
# → {"weather_source":"demo","llm_enabled":false,"web_search_enabled":false}
```

前端页面直接访问 `http://127.0.0.1:8000/`。

## 演示模式（无需任何密钥即可运行）

所有外部依赖走环境变量，**缺失时自动降级为确定性演示数据**，开关逻辑见
[app/backend/config.py](app/backend/config.py)：

| 环境变量 | 作用 | 缺失时行为 |
|---|---|---|
| `TJ_API_KEY` | 天机天气 API 密钥 | 使用确定性模拟天气（`weather_source: demo`），支持 `coldwave` / `rainstorm` 场景模拟突变 |
| `TJ_API_BASE` | 天气 API 地址 | 默认 `https://api.tjweather.com` |
| `MODELSCOPE_API_KEY` | 魔搭 LLM（OpenAI 兼容） | 报告用规则模板生成，不调用 LLM |
| `MODELSCOPE_BASE_URL` / `LLM_MODEL` | LLM 地址 / 模型 | 有默认值 |
| `TAVILY_API_KEY` | 联网搜索（装备参数，可选） | 跳过联网检索 |

运行状态可随时通过 `GET /api/meta` 查询。

## 其他约定

- 运行时数据写入 `app/backend/data/store/`（JSON 文件存储，已在 .gitignore 中排除，勿提交）。
- diff 引擎的提醒阈值集中在 `config.py` 的 `THRESHOLDS`，调整提醒灵敏度只改这里。
